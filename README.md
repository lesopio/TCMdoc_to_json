# txt-tojson
<img width="250" height="614" alt="image" src="https://github.com/user-attachments/assets/17f506eb-5ae7-4cf3-8dfd-b212759ff937" />

把《黄帝内经大词典》这类 TXT 文本拆分、清洗并导出为结构化 JSON 的小工具，包含：

- Python CLI 批处理脚本
- Flask 后端任务服务
- React + Vite 可视化面板
- SQLite 持久化存储
- 可选 LLM 精修

## 功能概览

- 自动发现并读取输入 TXT
- 拆分前言、词条目录、正文词条
- 解析词条、词性块、释义、引文示例
- 生成目录对账报告
- 将结果写入总 JSON、逐词条 JSON、SQLite 数据库
- 支持 `skip-llm`、`resume`、限制词条、只跑单词条
- Web 面板支持实时日志、进度、暂停、继续、下载导出文件

## 目录结构

```text
txt-tojson/
├─ parser_core.py          核心解析逻辑
├─ sqlite_store.py         SQLite 读写
├─ script.py               CLI 入口
├─ server.py               Flask API + 静态服务
├─ run_backend.py          启动后端
├─ run_frontend.py         启动前端
├─ run_fullstack.py        同时启动前后端
├─ frontend/               React 面板
└─ data/                   默认输出目录
```

## 环境要求

- Python 3.10+
- Node.js 18+
- npm

可选：

- `tiktoken`，用于更准确估算 prompt token
- OpenAI 兼容接口，用于 LLM 精修

## 安装

### 1. 安装 Python 依赖

这个仓库没有现成的 `requirements.txt`，按当前代码至少需要安装：

```bash
pip install flask
pip install tiktoken
```

如果你不需要 LLM 精修，也不关心 token 预估，`tiktoken` 可以不装。

### 2. 安装前端依赖

```bash
cd frontend
npm install
```

## 环境变量

在项目根目录放一个 `.env` 文件，后端会默认读取它。

最小示例：

```env
API_KEY=your_api_key
BASE_URL=https://your-openai-compatible-endpoint/v1
MODEL=gpt-4o-mini
TEMPERATURE=0
MAX_TOKENS=1200
CONCURRENCY=8
```

也支持 OpenAI 风格命名：

```env
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

也支持多组 LLM 配置并发轮询：

```env
API_KEY1=xxx
BASE_URL1=https://...
MODEL1=...

API_KEY2=yyy
BASE_URL2=https://...
MODEL2=...
```

相关变量说明：

- `API_KEY` / `OPENAI_API_KEY`：模型接口密钥
- `BASE_URL` / `OPENAI_BASE_URL`：兼容 OpenAI 的接口地址
- `MODEL` / `OPENAI_MODEL`：模型名
- `TEMPERATURE`：采样温度，默认 `0`
- `MAX_TOKENS`：单次输出上限，默认 `1200`
- `CONCURRENCY` / `LLM_CONCURRENCY` / `BATCH_SIZE`：默认并发数

如果没有配置可用的 API，程序仍可运行，但会退化为纯规则解析，或你可以显式传 `--skip-llm`。

## CLI 用法

### 最简单运行

如果当前目录下只有一个 `.txt` 文件：

```bash
python script.py --skip-llm
```

### 指定输入文件

```bash
python script.py --input "黄帝内经大词.txt"
```

### 常用参数

```bash
python script.py ^
  --input "黄帝内经大词.txt" ^
  --output "data/output.json" ^
  --staging "data/staging_rule_split.json" ^
  --review "data/review_queue.json" ^
  --catalog-report "data/catalog_report.json" ^
  --job-state "data/job_state.json" ^
  --entry-output-dir "data/entries" ^
  --database "data/parser.db" ^
  --env-file ".env" ^
  --limit 20 ^
  --only-term "天府" ^
  --concurrency 8 ^
  --resume
```

参数说明：

- `--input`：输入 TXT 路径
- `--output`：最终总 JSON 输出路径
- `--staging`：规则拆分阶段结果
- `--review`：待人工复核队列
- `--catalog-report`：目录对账报告
- `--job-state`：运行状态快照
- `--entry-output-dir`：逐词条 JSON 输出目录
- `--database`：SQLite 数据库路径
- `--env-file`：环境变量文件路径
- `--limit`：只处理前 N 个词条
- `--only-term`：只处理单个词条
- `--skip-llm`：跳过 LLM 精修
- `--resume`：基于已有 `output.json` 按词条续跑
- `--sleep`：每次 LLM 请求之间休眠秒数
- `--concurrency`：并发数

## Web 面板用法

### 分开启动

后端：

```bash
python run_backend.py
```

前端：

```bash
python run_frontend.py
```

或直接用批处理：

```bash
run_backend.bat
run_frontend.bat
```

### 一键启动前后端

```bash
python run_fullstack.py
```

或：

```bash
run_fullstack.bat
```

默认地址：

- 后端 API: `http://127.0.0.1:8000`
- 前端面板: `http://127.0.0.1:5173`

`run_fullstack.py` 会提示输入默认并发数，并分别注入：

- 后端环境变量 `DEFAULT_CONCURRENCY`
- 前端环境变量 `VITE_DEFAULT_CONCURRENCY`

## Web 面板功能

- 创建解析任务
- 指定输入文件、环境文件、词条过滤、限制条数
- 设置并发数和请求间隔
- 切换 `跳过 LLM` / `Resume`
- 实时查看 SSE 日志
- 查看词条进度、词性块进度
- 查看 token 汇总和最近一次模型输入输出预览
- 查看目录词条与正文词条对账结果
- 暂停任务 / 继续任务
- 下载最终导出文件

## API 概览

主要接口：

- `GET /api/health`：健康检查
- `POST /api/jobs`：创建任务
- `GET /api/jobs/<job_id>`：查询任务状态
- `POST /api/jobs/<job_id>/pause`：暂停任务
- `POST /api/jobs/<job_id>/resume`：继续任务
- `GET /api/jobs/<job_id>/events`：SSE 实时事件流
- `GET /api/jobs/<job_id>/results`：获取任务结果摘要
- `GET /api/jobs/<job_id>/catalog-report`：获取目录对账
- `GET /api/jobs/<job_id>/download/final-json`：下载总 JSON
- `GET /api/jobs/<job_id>/download/token-summary`：下载 token 汇总
- `GET /api/jobs/<job_id>/download/catalog-report`：下载目录对账报告
- `GET /api/jobs/<job_id>/download/entries-zip`：下载逐词条 ZIP

## 输出说明

默认输出在 `data/` 或 `data/jobs/<job_id>/` 下。

常见文件：

- `output.json`：最终总结果
- `staging_rule_split.json`：规则拆分中间结果
- `review_queue.json`：待人工复核项
- `catalog_report.json`：目录和正文的对账结果
- `job_state.json`：任务进度快照
- `entries/*.json`：逐词条导出
- `parser.db`：SQLite 数据库

任务完成后还可以导出：

- `final.json`：面向交付的总 JSON
- `token_summary.json`：token 统计
- `entries.zip`：逐词条压缩包

## 数据结构概览

单个词条大致长这样：

```json
{
  "term": "天府",
  "aliases": [],
  "pinyin": "",
  "normalized_term": "天府",
  "raw_header": "【天府】",
  "raw_text": "...",
  "senses": [
    {
      "pos": "名词",
      "definitions": ["..."],
      "examples": [
        {
          "source": "《灵枢》",
          "text": "..."
        }
      ],
      "raw_text": "...",
      "llm_used": true
    }
  ]
}
```

## 处理流程

1. 读取 TXT 并做基础归一化
2. 拆分前言、目录、正文
3. 提取目录词条并做对账
4. 从正文中识别词条块
5. 解析词性、释义、引文示例
6. 可选用 LLM 对词性块进行结构化精修
7. 落盘到 JSON、逐词条文件和 SQLite
8. 生成 review queue、catalog report、token summary

## Resume 说明

开启 `--resume` 后，程序会从已有 `output.json` 中读取已完成词条，并按词条名跳过重复处理，适合长任务中断后续跑。

注意：

- 如果你更换了输入文本，但仍复用旧的 `output.json`，可能导致结果混杂
- 更稳妥的做法是按不同输入文件或任务使用独立输出目录

## 常见问题

### 1. 启动时报 “Expected exactly one *.txt file in current directory”

你没有通过 `--input` 指定文件，并且当前目录下不是“恰好一个” `.txt` 文件。  
解决办法：显式传 `--input`。

### 2. 前端打开后无法创建任务

先确认后端已经启动，且地址是 `http://127.0.0.1:8000`。

### 3. npm 启动失败

确认本机已安装 Node.js，并且 `npm` 在 `PATH` 中。

### 4. LLM 不生效

检查：

- `.env` 是否存在
- `API_KEY` / `BASE_URL` / `MODEL` 是否正确
- 是否勾选或传入了 `skip-llm`

### 5. token 统计为空

如果接口没有返回 usage，或没安装 `tiktoken`，部分 token 数据可能为空或仅为估算。

## 适用场景

- 古籍辞典 TXT 结构化
- 术语词典清洗
- 词条级 JSON 归档
- 需要目录对账和人工复核队列的半自动解析流程

## 后续可补充

如果你准备长期维护这个项目，建议再补：

- `requirements.txt`
- `.env.example`
- 示例输入与示例输出
- 数据库表结构说明
- 单元测试

