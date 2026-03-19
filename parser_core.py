#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib import error, request

try:
    import tiktoken
except ImportError:  # pragma: no cover
    tiktoken = None

from sqlite_store import (
    connect_db,
    init_db,
    insert_llm_trace,
    load_job_catalog_report,
    load_job_entries,
    load_job_metrics,
    load_job_review_items,
    replace_catalog_terms,
    replace_entry,
    replace_review_items,
    upsert_job,
)


POS_LABELS = [
    "名词",
    "动词",
    "代词",
    "疑问代词",
    "副词",
    "形容词",
    "数量形容词",
    "助词",
    "介词",
    "连词",
    "数词",
    "量词",
    "词组",
    "术语",
    "古医经名",
    "经络名词",
    "生理名词",
    "病证名词",
    "病理名词",
    "篇名",
    "穴位名称",
    "穴位名词",
    "运气学说术语",
]

POS_PATTERN = "|".join(sorted((re.escape(item) for item in POS_LABELS), key=len, reverse=True))
ENTRY_START_RE = re.compile(r"(?m)^[#>\s]*[【\[]([^】\]\n]{1,40})[】\]]")
HEADING_ONLY_RE = re.compile(rf"^\s*(?:\d+\.\s*)?({POS_PATTERN})\s*$")
INLINE_HEADING_RE = re.compile(rf"^\s*(?:(\d+)\.\s*)?({POS_PATTERN})\s*(.+)$")
PINYIN_RE = re.compile(r"([A-Za-z][A-Za-z0-9\.\- ]{1,60})$")
ALIAS_RE = re.compile(r"[（(]([^()（）\n]+)[）)]")
SOURCE_RE = re.compile(r"(《[^》]{1,40}》)")
SOURCE_QUOTE_RE = re.compile(r"(《[^》]{1,40}》)\s*[：:]\s*([^《]+?)(?=(?:《[^》]{1,40}》\s*[：:])|$)")
PURE_PAGE_NOISE_RE = re.compile(r"(?m)^\s*(?:\d{1,4}|[A-Za-z]{1,4}\s*\d{1,4}|\d{1,4}\s*[A-Za-z]{1,4})\s*$")
BROKEN_GIBBERISH_RE = re.compile(r"(?m)^[^\u4e00-\u9fffA-Za-z0-9《》【】\[\]\(\)\.。\n]{6,}$")
CATALOG_HEADER_RE = re.compile(r"(?m)^\s*[．\.\- ]*\s*词条目录\s*$")
CATALOG_TERM_RE = re.compile(r"(?<![\u4e00-\u9fffA-Za-z])([\u4e00-\u9fffA-Za-z·]{1,18})\s+(\d{1,4})(?!\d)")

META_BODY_MARKERS = [
    "词条注解",
    "解释义项之后",
    "引录《黄帝内经》原文",
    "在词条后括号内标出",
    "多音字依次另列",
    "用字下加点的方式标出",
]

SYSTEM_PROMPT = """你是一个严谨的古汉语医学辞典结构化助手。
任务：把单个词条下的单个词性块清洗为 JSON。
要求：
1. 只能依据提供的原文，不补充、不猜测、不改写事实。
2. 输出必须是 JSON 对象，不要输出 Markdown。
3. JSON 结构固定为：
{
  "pos": "词性原文",
  "definitions": ["..."],
  "examples": [{"source": "《...》", "text": "..."}],
  "raw_text": "原始词性块文本"
}
4. definitions 只放解释性文字，不要把完整引文放进去。
5. examples 只放语境案例；如果能识别出处则填 source，否则填空字符串。
6. 如果没有解释或没有案例，返回空数组。
7. 保留原文中的术语，不做现代化改写。
"""


@dataclass
class Example:
    source: str
    text: str


@dataclass
class Sense:
    pos: str
    definitions: list[str] = field(default_factory=list)
    examples: list[Example] = field(default_factory=list)
    raw_text: str = ""
    llm_used: bool = False


@dataclass
class Entry:
    term: str
    aliases: list[str] = field(default_factory=list)
    pinyin: str = ""
    senses: list[Sense] = field(default_factory=list)
    raw_header: str = ""
    raw_text: str = ""
    normalized_term: str = ""


@dataclass
class ReviewItem:
    term: str
    pos: str
    stage: str
    reason: str
    raw_text: str


@dataclass
class CatalogReport:
    catalog_terms_total: int
    body_terms_total: int
    matched_terms: int
    match_rate: float
    catalog_only_terms: list[str]
    body_only_terms: list[str]
    catalog_terms: list[str]
    body_terms: list[str]


@dataclass
class RequestMetrics:
    estimated_prompt_tokens: int | None = None
    estimated_only: bool = False
    actual_prompt_tokens: int | None = None
    actual_completion_tokens: int | None = None
    actual_total_tokens: int | None = None
    request_duration_ms: int | None = None
    completion_tokens_per_sec: float | None = None
    total_tokens_per_sec: float | None = None


@dataclass
class LLMTrace:
    model: str
    term: str
    pos: str
    raw_text_length: int
    request_preview: str
    response_preview: str = ""
    error: str = ""
    retries: int = 0
    metrics: RequestMetrics = field(default_factory=RequestMetrics)


@dataclass
class ProcessingOptions:
    input_path: Path
    output_path: Path
    staging_path: Path
    review_path: Path
    catalog_report_path: Path
    job_state_path: Path | None = None
    entry_output_dir: Path | None = None
    database_path: Path | None = None
    job_id: str | None = None
    env_file: Path = Path(".env")
    limit: int | None = None
    only_term: str | None = None
    skip_llm: bool = False
    resume: bool = False
    sleep: float = 0.0
    concurrency: int | None = None
    pause_checker: PauseChecker | None = None
    state_getter: StateGetter | None = None
    output_dir: Path | None = None


@dataclass
class ProcessingResult:
    input_file: str
    entry_count: int
    skipped_by_resume: int
    llm_enabled: bool
    output_path: str
    entry_output_dir: str | None
    database_path: str | None
    staging_path: str
    review_path: str
    catalog_report_path: str
    entries: list[dict[str, Any]]
    review_queue: list[dict[str, Any]]
    catalog_report: dict[str, Any]
    metrics_summary: dict[str, Any]
    last_llm_trace: dict[str, Any] | None


@dataclass
class EntryTaskResult:
    entry_index: int
    entry_dict: dict[str, Any]
    traces: list[LLMTrace]
    review_items: list[ReviewItem]
    senses_count: int


Reporter = Callable[[dict[str, Any]], None]
PauseChecker = Callable[[], bool]
StateGetter = Callable[[], str]


def load_env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("'\"")
    return env


def build_client_env(env: dict[str, str], suffix: str) -> dict[str, str]:
    client_env = dict(env)
    if suffix:
        client_env["API_KEY"] = env.get(f"API_KEY{suffix}", env.get("API_KEY", ""))
        client_env["BASE_URL"] = env.get(f"BASE_URL{suffix}", env.get("BASE_URL", ""))
        client_env["MODEL"] = env.get(f"MODEL{suffix}", env.get("MODEL", ""))
        client_env["TEMPERATURE"] = env.get(f"TEMPERATURE{suffix}", env.get("TEMPERATURE", "0"))
        client_env["MAX_TOKENS"] = env.get(f"MAX_TOKENS{suffix}", env.get("MAX_TOKENS", "1200"))
    return client_env


def load_llm_clients(env: dict[str, str]) -> list["OpenAICompatClient"]:
    suffixes = []
    for key in env:
        match = re.fullmatch(r"API_KEY(\d*)", key)
        if match and env.get(key):
            suffixes.append(match.group(1))
    suffixes = sorted(set(suffixes), key=lambda item: (item != "", int(item or "1")))
    clients: list[OpenAICompatClient] = []
    for suffix in suffixes or [""]:
        client = OpenAICompatClient(build_client_env(env, suffix))
        if client.enabled:
            clients.append(client)
    return clients


def resolve_concurrency(options: ProcessingOptions, env: dict[str, str], llm_clients: list["OpenAICompatClient"]) -> int:
    if options.concurrency is not None and options.concurrency > 0:
        return options.concurrency
    for key in ("BATCH_SIZE", "LLM_CONCURRENCY", "CONCURRENCY"):
        value = env.get(key)
        if value:
            try:
                parsed = int(value)
            except ValueError:
                continue
            if parsed > 0:
                return parsed
    enabled_clients = max(1, len(llm_clients))
    return min(8, enabled_clients * 4)


def discover_input(path_arg: str | None) -> Path:
    if path_arg:
        path = Path(path_arg)
        if not path.exists():
            raise FileNotFoundError(f"Input file not found: {path}")
        return path
    txt_files = sorted(Path(".").glob("*.txt"))
    if len(txt_files) != 1:
        raise RuntimeError("Expected exactly one *.txt file in current directory")
    return txt_files[0]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned or "untitled"


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ")
    text = text.replace("［", "【").replace("］", "】")
    text = text.replace("[", "【").replace("]", "】")
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("．", ".")
    text = text.replace("· ", "·")
    text = re.sub(r"!\[\]\([^)]+\)\{[^}]+\}", "", text)
    text = re.sub(r"(?m)^\s*> ?", "", text)
    text = re.sub(r"(?m)^#{1,9}\s*", "", text)
    text = PURE_PAGE_NOISE_RE.sub("", text)
    text = BROKEN_GIBBERISH_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_term_key(term: str) -> str:
    cleaned = term.replace("【", "").replace("】", "")
    cleaned = cleaned.replace("(", "").replace(")", "")
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned.strip()


def split_sections(text: str) -> tuple[str, str, str]:
    catalog_match = CATALOG_HEADER_RE.search(text)
    if not catalog_match:
        return text, "", text
    preface = text[: catalog_match.start()].strip()
    tail = text[catalog_match.end() :].strip()
    body_match = ENTRY_START_RE.search(tail)
    if not body_match:
        return preface, tail, ""
    catalog = tail[: body_match.start()].strip()
    body = tail[body_match.start() :].strip()
    return preface, catalog, body


def clean_catalog_text(text: str) -> str:
    text = text.replace("|", " ").replace("+", " ").replace("=", " ").replace("-", " ")
    text = text.replace(">", " ")
    text = re.sub(r"(?m)^\s*[画頁页]+\s*$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def extract_catalog_terms(catalog_text: str) -> list[str]:
    cleaned = clean_catalog_text(catalog_text)
    terms: list[str] = []
    seen: set[str] = set()
    for match in CATALOG_TERM_RE.finditer(cleaned):
        term = match.group(1).strip(" .．")
        if len(term) <= 1 and not re.search(r"[\u4e00-\u9fff]{2,}", term):
            continue
        key = normalize_term_key(term)
        if not key or key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def split_entry_blocks(text: str) -> list[str]:
    matches = list(ENTRY_START_RE.finditer(text))
    if not matches:
        return []
    blocks: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks


def clean_header_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def clean_segment(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^\d+\.\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .")


def dedupe_texts(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for item in items:
        normalized = re.sub(r"\s+", "", item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        results.append(item.strip())
    return results


def dedupe_examples(items: Iterable[Example]) -> list[Example]:
    seen: set[tuple[str, str]] = set()
    results: list[Example] = []
    for item in items:
        key = (item.source.strip(), re.sub(r"\s+", "", item.text))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        results.append(item)
    return results


def extract_examples(raw_text: str) -> list[Example]:
    examples: list[Example] = []
    for line in raw_text.splitlines():
        text = clean_segment(line.lstrip(">"))
        if not text:
            continue
        matched_any = False
        for match in SOURCE_QUOTE_RE.finditer(text):
            matched_any = True
            source = match.group(1).strip()
            example_text = match.group(2).strip().strip("\"“”＂")
            if example_text:
                examples.append(Example(source=source, text=example_text))
        if matched_any:
            continue
        if text.startswith(("“", "\"", "＂")):
            example_text = text.strip().strip("\"“”＂")
            if example_text:
                examples.append(Example(source="", text=example_text))
    return dedupe_examples(examples)


def split_definition_candidates(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。；;])\s*", text)
    results = []
    for part in parts:
        candidate = part.strip(" ;；")
        if not candidate or len(candidate) <= 1:
            continue
        results.append(candidate)
    return results


def extract_definitions(raw_text: str) -> list[str]:
    definitions: list[str] = []
    for line in raw_text.splitlines():
        text = clean_segment(line.lstrip(">"))
        if not text:
            continue
        if SOURCE_RE.search(text):
            prefix = SOURCE_RE.split(text)[0].strip(" ：:，,")
            if prefix:
                definitions.extend(split_definition_candidates(prefix))
            continue
        if text.startswith("《"):
            continue
        definitions.extend(split_definition_candidates(text))
    return dedupe_texts(definitions)


def normalize_pos_label(line: str) -> str | None:
    normalized = re.sub(r"\s+", "", line)
    normalized = normalized.lstrip(">").strip()
    normalized = re.sub(r"^\d+\.", "", normalized)
    normalized = normalized.strip()
    ocr_map = {
        "动呈词": "动词",
        "动量词": "量词",
    }
    if normalized in ocr_map:
        return ocr_map[normalized]
    for label in sorted(POS_LABELS, key=len, reverse=True):
        if normalized == label or normalized.startswith(label):
            return label
    return None


def strip_pos_prefix(line: str, pos: str) -> str:
    stripped = line.strip()
    stripped = re.sub(r"^\d+\.\s*", "", stripped)
    if stripped.startswith(pos):
        return stripped[len(pos) :].strip()
    return ""


def parse_senses(body: str) -> list[Sense]:
    lines = [line.rstrip() for line in body.splitlines()]
    senses: list[Sense] = []
    current_pos: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_pos, buffer
        if current_pos is None:
            buffer = []
            return
        raw_text = "\n".join(line for line in buffer if line.strip()).strip()
        senses.append(
            Sense(
                pos=current_pos,
                definitions=extract_definitions(raw_text),
                examples=extract_examples(raw_text),
                raw_text=raw_text,
            )
        )
        buffer = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_pos is not None:
                buffer.append("")
            continue

        normalized_pos = normalize_pos_label(stripped)
        if normalized_pos:
            if current_pos is not None:
                flush()
            current_pos = normalized_pos
            remainder = strip_pos_prefix(stripped, normalized_pos)
            buffer = [remainder] if remainder else []
            continue

        inline_match = INLINE_HEADING_RE.match(stripped)
        if inline_match and stripped == inline_match.group(0).strip():
            if current_pos is not None:
                flush()
            current_pos = inline_match.group(2).strip()
            remainder = inline_match.group(3).strip()
            buffer = [remainder] if remainder else []
            continue

        heading_match = HEADING_ONLY_RE.fullmatch(stripped)
        if heading_match:
            if current_pos is not None:
                flush()
            current_pos = heading_match.group(1).strip()
            buffer = []
            continue

        if current_pos is None:
            continue
        buffer.append(stripped)

    if current_pos is not None:
        flush()
    return [sense for sense in senses if sense.raw_text or sense.definitions or sense.examples]


def parse_entry(block: str) -> Entry | None:
    lines = [line.rstrip() for line in block.splitlines() if line.strip()]
    if not lines:
        return None
    header = clean_header_line(lines[0])
    header_match = re.match(r"^[【\[]([^】\]]+)[】\]]\s*(.*)$", header)
    if not header_match:
        return None
    term = header_match.group(1).strip()
    tail = header_match.group(2).strip()
    aliases = [item.strip() for item in ALIAS_RE.findall(tail) if item.strip()]
    tail_wo_alias = ALIAS_RE.sub("", tail).strip()
    pinyin_match = PINYIN_RE.search(tail_wo_alias)
    pinyin = pinyin_match.group(1).strip() if pinyin_match else ""
    body = "\n".join(lines[1:]).strip()
    senses = parse_senses(body)
    if not senses and body:
        senses = [
            Sense(
                pos="未识别",
                definitions=extract_definitions(body),
                examples=extract_examples(body),
                raw_text=body,
            )
        ]
    return Entry(
        term=term,
        aliases=aliases,
        pinyin=pinyin,
        senses=senses,
        raw_header=header,
        raw_text=block.strip(),
        normalized_term=normalize_term_key(term),
    )


def is_meta_entry(entry: Entry) -> bool:
    body = "\n".join(sense.raw_text for sense in entry.senses).strip() or entry.raw_text
    if any(marker in body for marker in META_BODY_MARKERS):
        return True
    if "如：" in body and ("词条" in body or "原文" in body):
        return True
    return False


def parse_body_entries(body_text: str) -> list[Entry]:
    entry_blocks = split_entry_blocks(body_text)
    return [
        entry
        for block in entry_blocks
        if (entry := parse_entry(block)) and not is_meta_entry(entry)
    ]


def build_catalog_report(catalog_terms: list[str], entries: list[Entry]) -> CatalogReport:
    catalog_map = {normalize_term_key(term): term for term in catalog_terms}
    body_map = {entry.normalized_term: entry.term for entry in entries if entry.normalized_term}
    matched = sorted(set(catalog_map).intersection(body_map))
    catalog_only = [catalog_map[key] for key in sorted(set(catalog_map) - set(body_map))]
    body_only = [body_map[key] for key in sorted(set(body_map) - set(catalog_map))]
    match_rate = round(len(matched) / len(catalog_map), 4) if catalog_map else 0.0
    return CatalogReport(
        catalog_terms_total=len(catalog_map),
        body_terms_total=len(body_map),
        matched_terms=len(matched),
        match_rate=match_rate,
        catalog_only_terms=catalog_only,
        body_only_terms=body_only,
        catalog_terms=list(catalog_map.values()),
        body_terms=list(body_map.values()),
    )


def sense_to_dict(sense: Sense) -> dict[str, Any]:
    return {
        "pos": sense.pos,
        "definitions": sense.definitions,
        "examples": [asdict(example) for example in sense.examples],
        "raw_text": sense.raw_text,
        "llm_used": sense.llm_used,
    }


def entry_to_dict(entry: Entry) -> dict[str, Any]:
    return {
        "term": entry.term,
        "aliases": entry.aliases,
        "pinyin": entry.pinyin,
        "senses": [sense_to_dict(sense) for sense in entry.senses],
        "raw_header": entry.raw_header,
        "raw_text": entry.raw_text,
        "normalized_term": entry.normalized_term,
    }


def review_to_dict(item: ReviewItem) -> dict[str, Any]:
    return asdict(item)


def read_existing_entries(path: Path) -> dict[str, dict[str, Any]]:
    data = read_json(path)
    if not data:
        return {}
    entries = data if isinstance(data, list) else data.get("entries", [])
    mapping: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("term"):
            mapping[str(entry["term"])] = entry
    return mapping


def read_existing_review_items(path: Path) -> list[ReviewItem]:
    data = read_json(path)
    if not isinstance(data, list):
        return []
    items: list[ReviewItem] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        items.append(
            ReviewItem(
                term=str(item.get("term", "")),
                pos=str(item.get("pos", "")),
                stage=str(item.get("stage", "")),
                reason=str(item.get("reason", "")),
                raw_text=str(item.get("raw_text", "")),
            )
        )
    return items


def merge_metrics_summaries(base: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    base = base or {}
    estimated = (base.get("estimated_prompt_tokens") or 0) + (current.get("estimated_prompt_tokens") or 0)
    actual_prompt = (base.get("actual_prompt_tokens") or 0) + (current.get("actual_prompt_tokens") or 0)
    actual_completion = (base.get("actual_completion_tokens") or 0) + (current.get("actual_completion_tokens") or 0)
    actual_total = (base.get("actual_total_tokens") or 0) + (current.get("actual_total_tokens") or 0)
    duration_ms = (base.get("request_duration_ms") or 0) + (current.get("request_duration_ms") or 0)
    llm_calls = (base.get("llm_calls") or 0) + (current.get("llm_calls") or 0)
    completion_speed = round(actual_completion / (duration_ms / 1000), 2) if actual_completion and duration_ms else None
    total_speed = round(actual_total / (duration_ms / 1000), 2) if actual_total and duration_ms else None
    return {
        "estimated_prompt_tokens": estimated or 0,
        "actual_prompt_tokens": actual_prompt or None,
        "actual_completion_tokens": actual_completion or None,
        "actual_total_tokens": actual_total or None,
        "request_duration_ms": duration_ms or None,
        "completion_tokens_per_sec": completion_speed,
        "total_tokens_per_sec": total_speed,
        "llm_calls": llm_calls,
        "estimated_only": bool(base.get("estimated_only")) or bool(current.get("estimated_only")),
    }


class OpenAICompatClient:
    def __init__(self, env: dict[str, str], timeout: int = 120) -> None:
        self.api_key = env.get("API_KEY") or env.get("OPENAI_API_KEY", "")
        self.base_url = (
            env.get("BASE_URL")
            or env.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = env.get("MODEL") or env.get("OPENAI_MODEL") or ""
        self.temperature = float(env.get("TEMPERATURE", "0"))
        self.max_tokens = int(env.get("MAX_TOKENS", "1200"))
        self.timeout = timeout
        self.encoding = self._load_encoding()

    def _load_encoding(self) -> Any:
        if tiktoken is None:
            return None
        try:
            if self.model:
                return tiktoken.encoding_for_model(self.model)
        except Exception:  # noqa: BLE001
            pass
        try:
            return tiktoken.get_encoding("o200k_base")
        except Exception:  # noqa: BLE001
            return None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)

    def estimate_tokens(self, text: str) -> tuple[int | None, bool]:
        if self.encoding is not None:
            return len(self.encoding.encode(text)), False
        return max(1, len(text) // 4), True

    def chat_json(self, payload_text: str, trace: LLMTrace) -> tuple[dict[str, Any], LLMTrace]:
        if not self.enabled:
            raise RuntimeError("LLM is not configured")

        estimated_tokens, estimated_only = self.estimate_tokens(payload_text + SYSTEM_PROMPT)
        trace.metrics.estimated_prompt_tokens = estimated_tokens
        trace.metrics.estimated_only = estimated_only

        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": payload_text},
            ],
        }
        req = request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                response_body = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            trace.error = f"HTTP {exc.code}: {detail}"
            raise RuntimeError(trace.error) from exc
        except error.URLError as exc:
            trace.error = f"Network error: {exc}"
            raise RuntimeError(trace.error) from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        trace.metrics.request_duration_ms = duration_ms
        payload = json.loads(response_body)
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {}) or {}
        trace.response_preview = content[:4000]
        trace.metrics.actual_prompt_tokens = usage.get("prompt_tokens")
        trace.metrics.actual_completion_tokens = usage.get("completion_tokens")
        trace.metrics.actual_total_tokens = usage.get("total_tokens")
        if duration_ms > 0 and trace.metrics.actual_completion_tokens:
            trace.metrics.completion_tokens_per_sec = round(
                trace.metrics.actual_completion_tokens / (duration_ms / 1000), 2
            )
        if duration_ms > 0 and trace.metrics.actual_total_tokens:
            trace.metrics.total_tokens_per_sec = round(
                trace.metrics.actual_total_tokens / (duration_ms / 1000), 2
            )
        return json.loads(content), trace


def llm_payload(entry: Entry, sense: Sense) -> str:
    return json.dumps(
        {
            "term": entry.term,
            "aliases": entry.aliases,
            "pinyin": entry.pinyin,
            "pos": sense.pos,
            "raw_text": sense.raw_text,
            "rule_based": {
                "definitions": sense.definitions,
                "examples": [asdict(example) for example in sense.examples],
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def validate_llm_sense(data: dict[str, Any], fallback: Sense) -> Sense:
    pos = str(data.get("pos", "")).strip() or fallback.pos
    raw_text = str(data.get("raw_text", "")).strip() or fallback.raw_text
    definitions_raw = data.get("definitions", [])
    if not isinstance(definitions_raw, list):
        raise ValueError("definitions is not a list")
    definitions = dedupe_texts(str(item).strip() for item in definitions_raw if str(item).strip())
    examples_raw = data.get("examples", [])
    if not isinstance(examples_raw, list):
        raise ValueError("examples is not a list")
    examples: list[Example] = []
    for item in examples_raw:
        if not isinstance(item, dict):
            raise ValueError("example item is not an object")
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        source = str(item.get("source", "")).strip()
        examples.append(Example(source=source, text=text))
    return Sense(
        pos=pos,
        definitions=definitions,
        examples=dedupe_examples(examples),
        raw_text=raw_text,
        llm_used=True,
    )


def build_trace(entry: Entry, sense: Sense, llm: OpenAICompatClient) -> LLMTrace:
    payload = llm_payload(entry, sense)
    return LLMTrace(
        model=llm.model,
        term=entry.term,
        pos=sense.pos,
        raw_text_length=len(sense.raw_text),
        request_preview=payload[:4000],
    )


def process_entry_task(
    entry_index: int,
    entry: Entry,
    llm_clients: list[OpenAICompatClient],
    skip_llm: bool,
    sleep_seconds: float,
) -> EntryTaskResult:
    refined_senses: list[Sense] = []
    traces: list[LLMTrace] = []
    review_items: list[ReviewItem] = []

    for sense_index, sense in enumerate(entry.senses):
        if skip_llm or not llm_clients or sense.pos == "未识别":
            refined_senses.append(sense)
            if sense.pos == "未识别":
                review_items.append(
                    ReviewItem(
                        term=entry.term,
                        pos=sense.pos,
                        stage="rule",
                        reason="未识别词性，保留规则结果",
                        raw_text=sense.raw_text,
                    )
                )
            continue

        client = llm_clients[(entry_index + sense_index) % len(llm_clients)]
        trace = build_trace(entry, sense, client)
        try:
            payload = llm_payload(entry, sense)
            result, trace = client.chat_json(payload, trace)
            refined_senses.append(validate_llm_sense(result, sense))
        except Exception as exc:  # noqa: BLE001
            trace.error = str(exc)
            refined_senses.append(sense)
            review_items.append(
                ReviewItem(
                    term=entry.term,
                    pos=sense.pos,
                    stage="llm",
                    reason=f"LLM failed, fallback to rule result: {exc}",
                    raw_text=sense.raw_text,
                )
            )
        traces.append(trace)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    final_entry = Entry(
        term=entry.term,
        aliases=entry.aliases,
        pinyin=entry.pinyin,
        senses=refined_senses,
        raw_header=entry.raw_header,
        raw_text=entry.raw_text,
        normalized_term=entry.normalized_term,
    )
    return EntryTaskResult(
        entry_index=entry_index,
        entry_dict=entry_to_dict(final_entry),
        traces=traces,
        review_items=review_items,
        senses_count=len(entry.senses),
    )


def summarize_metrics(traces: list[LLMTrace]) -> dict[str, Any]:
    estimated = sum(trace.metrics.estimated_prompt_tokens or 0 for trace in traces)
    actual_prompt = sum(trace.metrics.actual_prompt_tokens or 0 for trace in traces)
    actual_completion = sum(trace.metrics.actual_completion_tokens or 0 for trace in traces)
    actual_total = sum(trace.metrics.actual_total_tokens or 0 for trace in traces)
    duration_ms = sum(trace.metrics.request_duration_ms or 0 for trace in traces)
    completion_speed = round(actual_completion / (duration_ms / 1000), 2) if actual_completion and duration_ms else None
    total_speed = round(actual_total / (duration_ms / 1000), 2) if actual_total and duration_ms else None
    return {
        "estimated_prompt_tokens": estimated,
        "actual_prompt_tokens": actual_prompt or None,
        "actual_completion_tokens": actual_completion or None,
        "actual_total_tokens": actual_total or None,
        "request_duration_ms": duration_ms or None,
        "completion_tokens_per_sec": completion_speed,
        "total_tokens_per_sec": total_speed,
        "llm_calls": len(traces),
        "estimated_only": any(trace.metrics.estimated_only for trace in traces),
    }


def default_reporter(_: dict[str, Any]) -> None:
    return


def snapshot_state(path: Path | None, payload: dict[str, Any]) -> None:
    if path is not None:
        write_json(path, payload)


def current_runtime_state(options: ProcessingOptions) -> str:
    if options.state_getter is not None:
        return options.state_getter()
    return "running"


def is_runtime_paused(options: ProcessingOptions) -> bool:
    if options.pause_checker is None:
        return False
    return options.pause_checker()


def process_document(options: ProcessingOptions, reporter: Reporter | None = None) -> ProcessingResult:
    reporter = reporter or default_reporter
    if options.entry_output_dir is None:
        options.entry_output_dir = options.output_path.parent / "entries"
    options.entry_output_dir.mkdir(parents=True, exist_ok=True)
    if options.database_path is None:
        options.database_path = options.output_path.parent / "parser.db"
    db_conn = connect_db(options.database_path)
    init_db(db_conn)
    env = load_env(options.env_file)
    llm_clients = load_llm_clients(env)
    llm_enabled = bool(llm_clients)
    concurrency = 1 if options.skip_llm or not llm_enabled else resolve_concurrency(options, env, llm_clients)
    job_id = options.job_id or uuid.uuid4().hex
    raw_text = options.input_path.read_text(encoding="utf-8", errors="replace")
    normalized = normalize_text(raw_text)
    preface_text, catalog_text, body_text = split_sections(normalized)
    catalog_terms = extract_catalog_terms(catalog_text)
    entries = parse_body_entries(body_text)

    if options.only_term:
        entries = [entry for entry in entries if entry.term == options.only_term]
        catalog_terms = [term for term in catalog_terms if normalize_term_key(term) == normalize_term_key(options.only_term)]
    if options.limit is not None:
        entries = entries[: options.limit]

    catalog_report = build_catalog_report(catalog_terms, entries)
    upsert_job(
        db_conn,
        job_id=job_id,
        input_file=str(options.input_path),
        state="running",
        llm_enabled=llm_enabled and not options.skip_llm,
        only_term=options.only_term,
        limit_count=options.limit,
        skip_llm=options.skip_llm,
        resume_enabled=options.resume,
        metrics_json={},
        catalog_report_json=asdict(catalog_report),
    )
    replace_catalog_terms(
        db_conn,
        job_id,
        [(term, normalize_term_key(term)) for term in catalog_terms],
    )
    staging_payload = {
        "input_file": str(options.input_path),
        "preface_length": len(preface_text),
        "catalog_length": len(catalog_text),
        "body_length": len(body_text),
        "catalog_terms": catalog_terms,
        "catalog_report": asdict(catalog_report),
        "entry_count": len(entries),
        "entries": [entry_to_dict(entry) for entry in entries],
    }
    write_json(options.staging_path, staging_payload)
    write_json(options.catalog_report_path, asdict(catalog_report))

    existing_payload = read_json(options.output_path) if options.resume else None
    base_metrics_summary = {}
    if isinstance(existing_payload, dict):
        base_metrics_summary = existing_payload.get("metrics_summary", {}) or {}
    review_queue: list[ReviewItem] = read_existing_review_items(options.review_path) if options.resume else []
    existing_entries = read_existing_entries(options.output_path) if options.resume else {}
    final_entries: list[dict[str, Any]] = []
    skipped_by_resume = 0
    traces: list[LLMTrace] = []
    last_trace: LLMTrace | None = None

    total_entries = len(entries)
    total_senses = sum(len(entry.senses) for entry in entries)
    entries_processed = 0
    senses_processed = 0

    reporter(
        {
            "type": "job_started",
            "entries_total": total_entries,
            "senses_total": total_senses,
            "llm_enabled": llm_enabled and not options.skip_llm,
            "concurrency": concurrency,
            "client_count": len(llm_clients),
        }
    )
    partial_output_payload = {
        "job_id": job_id,
        "input_file": str(options.input_path),
        "entry_count": 0,
        "skipped_by_resume": skipped_by_resume,
        "llm_enabled": llm_enabled and not options.skip_llm,
        "concurrency": concurrency,
        "client_count": len(llm_clients),
        "entry_output_dir": str(options.entry_output_dir),
        "database_path": str(options.database_path),
        "catalog_report": asdict(catalog_report),
        "metrics_summary": merge_metrics_summaries(base_metrics_summary, summarize_metrics(traces)),
        "entries": [],
    }
    write_json(options.output_path, partial_output_payload)
    write_json(options.review_path, [review_to_dict(item) for item in review_queue])
    snapshot_state(
        options.job_state_path,
        {
            "state": current_runtime_state(options),
            "entries_total": total_entries,
            "entries_processed": 0,
            "senses_total": total_senses,
            "senses_processed": 0,
            "completed_terms": [],
            "concurrency": concurrency,
            "client_count": len(llm_clients),
            "catalog_report": asdict(catalog_report),
        },
    )

    ordered_entries: list[dict[str, Any] | None] = [None] * total_entries
    pending_work: list[tuple[int, Entry]] = []

    for entry_index, entry in enumerate(entries):
        if options.resume and entry.term in existing_entries:
            ordered_entries[entry_index] = existing_entries[entry.term]
            skipped_by_resume += 1
            entries_processed += 1
            senses_processed += len(entry.senses)
            merged_metrics_summary = merge_metrics_summaries(base_metrics_summary, summarize_metrics(traces))
            partial_output_payload["entries"] = [item for item in ordered_entries if item is not None]
            partial_output_payload["entry_count"] = len(partial_output_payload["entries"])
            partial_output_payload["skipped_by_resume"] = skipped_by_resume
            partial_output_payload["metrics_summary"] = merged_metrics_summary
            write_json(options.output_path, partial_output_payload)
            reporter(
                {
                    "type": "entry_skipped",
                    "term": entry.term,
                    "entries_processed": entries_processed,
                    "entries_total": total_entries,
                    "senses_processed": senses_processed,
                    "senses_total": total_senses,
                    "metrics_summary": merged_metrics_summary,
                }
            )
            upsert_job(
                db_conn,
                job_id=job_id,
                input_file=str(options.input_path),
                state="running",
                llm_enabled=llm_enabled and not options.skip_llm,
                only_term=options.only_term,
                limit_count=options.limit,
                skip_llm=options.skip_llm,
                resume_enabled=options.resume,
                metrics_json=merged_metrics_summary,
                catalog_report_json=asdict(catalog_report),
            )
            continue
        pending_work.append((entry_index, entry))

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        pending_queue = deque(pending_work)
        future_map: dict[Any, tuple[int, Entry]] = {}

        while pending_queue or future_map:
            while len(future_map) < max(1, concurrency) and pending_queue and not is_runtime_paused(options):
                entry_index, entry = pending_queue.popleft()
                future = executor.submit(
                    process_entry_task,
                    entry_index,
                    entry,
                    llm_clients,
                    options.skip_llm,
                    options.sleep,
                )
                future_map[future] = (entry_index, entry)

            if is_runtime_paused(options) and not future_map:
                time.sleep(0.2)
                continue

            if not future_map:
                continue

            done, _ = wait(list(future_map.keys()), timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                entry_index, entry = future_map.pop(future)
                task_result = future.result()
                ordered_entries[entry_index] = task_result.entry_dict
                final_entry_dict = task_result.entry_dict

                for trace in task_result.traces:
                    traces.append(trace)
                    last_trace = trace
                    insert_llm_trace(db_conn, job_id, asdict(trace))

                if task_result.review_items:
                    review_queue.extend(task_result.review_items)
                    replace_review_items(db_conn, job_id, [review_to_dict(item) for item in review_queue])

                entry_file = options.entry_output_dir / f"{safe_filename(final_entry_dict['term'])}.json"
                write_json(entry_file, final_entry_dict)
                replace_entry(db_conn, job_id, final_entry_dict, str(entry_file))

                entries_processed += 1
                senses_processed += task_result.senses_count
                merged_metrics_summary = merge_metrics_summaries(base_metrics_summary, summarize_metrics(traces))
                partial_output_payload["entries"] = [item for item in ordered_entries if item is not None]
                partial_output_payload["entry_count"] = len(partial_output_payload["entries"])
                partial_output_payload["skipped_by_resume"] = skipped_by_resume
                partial_output_payload["metrics_summary"] = merged_metrics_summary
                write_json(options.output_path, partial_output_payload)
                write_json(options.review_path, [review_to_dict(item) for item in review_queue])

                upsert_job(
                    db_conn,
                    job_id=job_id,
                    input_file=str(options.input_path),
                    state=current_runtime_state(options),
                    llm_enabled=llm_enabled and not options.skip_llm,
                    only_term=options.only_term,
                    limit_count=options.limit,
                    skip_llm=options.skip_llm,
                    resume_enabled=options.resume,
                    metrics_json=merged_metrics_summary,
                    catalog_report_json=asdict(catalog_report),
                )

                state_payload = {
                    "state": current_runtime_state(options),
                    "entries_total": total_entries,
                    "entries_processed": entries_processed,
                    "senses_total": total_senses,
                    "senses_processed": senses_processed,
                    "completed_terms": [entry_dict["term"] for entry_dict in partial_output_payload["entries"]],
                    "metrics_summary": merged_metrics_summary,
                    "last_llm_trace": asdict(last_trace) if last_trace else None,
                    "catalog_report": asdict(catalog_report),
                    "concurrency": concurrency,
                    "client_count": len(llm_clients),
                }
                reporter({"type": "entry_completed", "term": entry.term, **state_payload})
                snapshot_state(options.job_state_path, state_payload)

    final_entries = [item for item in ordered_entries if item is not None]

    output_payload = {
        "job_id": partial_output_payload["job_id"],
        "input_file": str(options.input_path),
        "entry_count": len(final_entries),
        "skipped_by_resume": skipped_by_resume,
        "llm_enabled": llm_enabled and not options.skip_llm,
        "concurrency": concurrency,
        "client_count": len(llm_clients),
        "entry_output_dir": str(options.entry_output_dir),
        "database_path": str(options.database_path),
        "catalog_report": asdict(catalog_report),
        "metrics_summary": merge_metrics_summaries(base_metrics_summary, summarize_metrics(traces)),
        "entries": final_entries,
    }
    write_json(options.output_path, output_payload)
    write_json(options.review_path, [review_to_dict(item) for item in review_queue])

    metrics_summary = merge_metrics_summaries(base_metrics_summary, summarize_metrics(traces))
    upsert_job(
        db_conn,
        job_id=job_id,
        input_file=str(options.input_path),
        state="completed",
        llm_enabled=llm_enabled and not options.skip_llm,
        only_term=options.only_term,
        limit_count=options.limit,
        skip_llm=options.skip_llm,
        resume_enabled=options.resume,
        metrics_json=metrics_summary,
        catalog_report_json=asdict(catalog_report),
    )
    final_state = {
        "state": "completed",
        "entries_total": total_entries,
        "entries_processed": entries_processed,
        "senses_total": total_senses,
        "senses_processed": senses_processed,
        "completed_terms": [entry["term"] for entry in final_entries],
        "metrics_summary": metrics_summary,
        "last_llm_trace": asdict(last_trace) if last_trace else None,
        "catalog_report": asdict(catalog_report),
        "concurrency": concurrency,
        "client_count": len(llm_clients),
        "database_path": str(options.database_path),
        "output_path": str(options.output_path),
        "staging_path": str(options.staging_path),
        "review_path": str(options.review_path),
        "catalog_report_path": str(options.catalog_report_path),
    }
    snapshot_state(options.job_state_path, final_state)
    reporter({"type": "job_completed", **final_state})

    return ProcessingResult(
        input_file=str(options.input_path),
        entry_count=len(final_entries),
        skipped_by_resume=skipped_by_resume,
        llm_enabled=llm_enabled and not options.skip_llm,
        output_path=str(options.output_path),
        entry_output_dir=str(options.entry_output_dir),
        database_path=str(options.database_path),
        staging_path=str(options.staging_path),
        review_path=str(options.review_path),
        catalog_report_path=str(options.catalog_report_path),
        entries=final_entries,
        review_queue=[review_to_dict(item) for item in review_queue],
        catalog_report=asdict(catalog_report),
        metrics_summary=metrics_summary,
        last_llm_trace=asdict(last_trace) if last_trace else None,
    )
