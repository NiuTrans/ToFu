"""routes/paper.py — Paper Reading Mode endpoints.

Provides:
- /api/paper/chat      — Streaming LLM chat for paper Q&A and translation
- /api/paper/report    — Single-model streaming deep analysis report
- /api/paper/report/cache — Lookup cached report by paper hash
- /api/paper/fetch-arxiv — Fetch PDF from arXiv URL and return a serveable path
- /api/paper/upload    — Upload a PDF for reading
"""

import hashlib
import json
import os
import queue
import re
import threading
import time

import requests as _requests
from flask import Blueprint, Response, jsonify, request, send_file

import lib as _lib
from lib.log import get_logger
from lib.llm_client import build_body
from lib.database import get_db, get_thread_db, db_execute_with_retry
from lib.llm_dispatch.api import dispatch_stream
from lib.tools.search import SEARCH_TOOL_MULTI, FETCH_URL_TOOL
from lib.search.orchestrator import perform_web_search
from lib.fetch import fetch_page_content

logger = get_logger(__name__)

paper_bp = Blueprint('paper', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_DIR = os.path.join(BASE_DIR, 'uploads', 'papers')
PAPER_IMG_DIR = os.path.join(PAPER_DIR, 'images')
os.makedirs(PAPER_DIR, exist_ok=True)
os.makedirs(PAPER_IMG_DIR, exist_ok=True)


def _paper_hash(text):
    """Compute a stable hash of the paper text for DB caching."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def _safe_hash_dir(phash):
    """Validate/normalize a paper hash to prevent path traversal.

    Returns the 32-char hex string or None if invalid.
    """
    if not phash or not isinstance(phash, str):
        return None
    if not re.fullmatch(r'[a-f0-9]{8,64}', phash):
        return None
    return phash


# ══════════════════════════════════════════════════════
#  Report prompt — single comprehensive analysis
# ══════════════════════════════════════════════════════

_REPORT_PROMPT_EN = """\
You are a senior research scientist writing a comprehensive analysis report for an academic paper.
Read the paper below carefully and produce a **complete, structured Markdown report** covering all of the following sections in order.

Write the full report in one pass. Be specific, quantitative, and analytical — not vague or superficial. Cite actual numbers, method names, and benchmarks from the paper.

## 🧮 Formatting rules — READ CAREFULLY

1. **Math** — ALL mathematical notation MUST use KaTeX delimiters so the reader's browser can render it:
   - Inline math: `$E = mc^2$`, `$d_{\\text{model}}=512$`, `$\\sqrt{d_k}$`, `$\\mathcal{O}(n^2)$`
   - Display/block math (own line): `$$\\text{Attention}(Q,K,V) = \\text{softmax}\\left(\\frac{QK^\\top}{\\sqrt{d_k}}\\right)V$$`
   - **Never** wrap math in backticks (e.g. `` `d_k=64` ``) — backticks render as gray code, not formulas.
   - Inside table cells, keep math in `$...$`. For literal `|` inside math use `\\vert` or `\\mid`.

2. **Figures / tables from the paper** — you are provided a manifest of images extracted from the paper (below the paper text). For each figure or table you discuss, embed the image inline using Markdown syntax, placing it right before or after the paragraph that discusses it:
   ```
   ![Figure 3 — Transformer architecture](IMG_URL_FROM_MANIFEST)
   ```
   Use the **exact** URL given in the manifest. Only embed images that are relevant to the section you are currently writing. Do not invent URLs. If the manifest is empty, skip images silently.

---

## ⚡ TL;DR
2-3 crisp sentences: what they did, the key result, and why it matters. Include specific method names, numbers, and benchmarks. A busy professor should get the full picture in 10 seconds.

## 📋 Paper Card
| Field | Detail |
|-------|--------|
| **Title** | (full title) |
| **Authors** | (first author et al., or all if ≤4) |
| **Affiliation** | (primary institutions) |
| **Venue / Year** | (conference/journal, year — infer if needed) |
| **arXiv / DOI** | (if identifiable) |
| **Code / Data** | (any URLs mentioned) |

## 🎯 Problem & Motivation
1. The specific problem — the exact gap or limitation being addressed.
2. Why existing approaches fail — cite specific prior methods, explain their shortcomings concretely.
3. Real-world impact — who benefits and how.
4. The key insight that enables their approach.

## 💡 Method — How It Works
### Core Insight
The central idea in 2-3 sentences.

### Architecture / Pipeline
Step-by-step walkthrough with numbered steps. For each: input, output, what operation happens, and what makes it different from the naive approach.

### Novel Components vs. Borrowed
Explicitly separate new contributions from adopted/standard components.

### Key Design Choices & Trade-offs
For each important decision: what was chosen, what were alternatives, why, and what trade-offs.

### Training & Optimization Details
Loss functions, training data, hyperparameters, engineering tricks.

## 📊 Experimental Analysis
### Main Results
Compact comparison table:
| Benchmark / Task | Their Method | Best Baseline | Δ Improvement |
|------------------|-------------|---------------|---------------|
| ... | ... | ... | ... |

### Experimental Setup
Datasets, metrics, baselines, compute resources.

### Deep Dive
Where the method shines, where it struggles, surprising findings, consistency of results.

### Ablation Studies
Which components contribute most, which are unimportant, diminishing returns patterns.

### What's Missing
Experiments you'd want to see, omitted baselines, fairness of comparisons.

## ✅ Strengths
5-7 bullet points. For each, explain WHY it's a strength. Consider novelty, experiment thoroughness, clarity, theoretical grounding, reproducibility.

## ⚠️ Weaknesses & Limitations
5-7 bullet points. Be honest but constructive. State the weakness, its impact on claims, and how it could be addressed.

## 🗺️ Research Landscape & Impact
### Positioning
Where this paper sits in the research timeline. Comparison with closest prior work.

### Intellectual Lineage
2-3 key ancestor ideas this builds upon.

### Impact Assessment
Likely impact (transformative/incremental/niche), downstream applications, societal concerns.

### Future Directions
Most promising next step, a risky high-reward extension, connections to emerging trends.

## 📝 Technical Reference
### Key Concepts & Glossary
| Term | Definition |
|------|-----------|
| (8-12 domain-specific terms) | (clear definitions) |

### Key Equations & Theorems
Most important formulations with plain-language explanations.

### Reproducibility Checklist
- [ ] Code available?
- [ ] Data available?
- [ ] Hyperparameters fully specified?
- [ ] Compute requirements stated?
- [ ] Random seeds / variance reported?

---

Write in the same language as the paper. Be thorough but concise — aim for quality over length.

Paper text:
{paper_text}"""

_REPORT_PROMPT_ZH = """\
你是一位资深研究科学家，正在为一篇学术论文撰写全面的分析报告。
请仔细阅读下面的论文，按照以下所有章节顺序，**一次性生成完整的 Markdown 结构化报告**。

要求：具体、量化、有分析深度，不要空泛笼统。引用论文中的实际数据、方法名和基准测试。

## 🧮 格式规范（必须严格遵守）

1. **数学公式** — 所有数学符号必须使用 KaTeX 定界符，便于浏览器渲染：
   - 行内公式：`$E = mc^2$`、`$d_{\\text{model}}=512$`、`$\\sqrt{d_k}$`、`$\\mathcal{O}(n^2)$`
   - 独立公式（单独成行）：`$$\\text{Attention}(Q,K,V) = \\text{softmax}\\left(\\frac{QK^\\top}{\\sqrt{d_k}}\\right)V$$`
   - **严禁**用反引号包住公式（例如 `` `d_k=64` ``）——反引号会被渲染成灰色代码而不是公式。
   - 表格单元格内也用 `$...$`；若公式中需要字面量 `|`，改写为 `\\vert` 或 `\\mid`。

2. **论文中的图表** — 系统已从论文中抽取了一批图/表图像，清单见"论文正文"下方的"Image manifest"部分。你在讲解某张图/表时，请在对应段落前后用 Markdown 嵌入图片：
   ```
   ![图 3 — Transformer 架构](清单中给出的图片 URL)
   ```
   URL 必须**照抄**清单里给出的地址，不要臆造；只嵌入与当前讨论相关的图；若清单为空则不嵌图。

---

## ⚡ 一句话总结
2-3 句话精炼概括：他们做了什么，关键结果是什么，为什么重要。包含具体方法名、数字和基准。让忙碌的教授 10 秒内掌握全貌。

## 📋 论文信息卡
| 字段 | 内容 |
|------|------|
| **标题** | （完整标题） |
| **作者** | （第一作者 et al.，或全部作者（≤4人时）） |
| **机构** | （主要机构） |
| **发表会议/期刊 / 年份** | （会议/期刊名，年份——可推断） |
| **arXiv / DOI** | （如能识别） |
| **代码 / 数据** | （论文中提到的任何仓库或数据集链接） |

## 🎯 问题与动机
1. **具体问题** — 不是泛泛的研究领域，而是该论文要解决的精确缺口或局限。
2. **现有方法为何失败** — 引用论文中提到的具体先前方法，用实例解释它们的不足。不要只说"已有工作有局限"，要说清楚什么方法在什么情况下失败了、为什么。
3. **现实影响** — 谁会受益？解决这个问题能改变什么？具体说明应用场景。
4. **核心洞察** — 是什么关键观察或假设让他们的方案成为可能？

## 💡 方法详解
### 核心思想
2-3 句话说清中心思想，"灵光一现"在哪里。

### 架构 / 流程
分步骤描述方法（编号列表）。每一步说明：
- 输入和输出是什么？
- 具体执行了什么操作？（不要含糊——要精确）
- 与朴素/直觉方法有什么不同？

### 新贡献 vs. 借鉴
明确区分：
- 哪些是本文的**新贡献**
- 哪些是**借鉴/改编**自前人工作（注明出处）
- 哪些是**标准组件**（如"标准 Transformer 编码器"）

### 关键设计选择与权衡
对每个重要决策：选了什么、有哪些备选方案、为什么这样选、有什么权衡。

### 训练与优化细节
损失函数及其直觉、训练数据、数据增强、关键超参数、工程技巧。

## 📊 实验分析
### 主要结果
核心对比表格：
| 基准/任务 | 本文方法 | 最强基线 | 提升幅度 |
|-----------|---------|---------|---------|
| ... | ... | ... | ... |

### 实验设置
数据集（名称、规模、领域）、评估指标、对比基线、计算资源。

### 结果深入分析
方法在哪些任务/数据集上表现突出？在哪些方面较弱？有无意外发现或矛盾之处？

### 消融实验
哪个组件贡献最大、哪些出乎意料地不重要、有无边际递减。

### 缺失的实验
你还想看到什么实验？是否遗漏了明显的基线或数据集？对比是否公平？

## ✅ 优点
5-7 个要点，每个都解释**为什么**这是优点（不是只列现象）。考虑新颖性、实验充分性、表述清晰度、理论基础、可复现性。

## ⚠️ 不足与局限
5-7 个要点。坦诚但有建设性。每个要点说清不足本身、对论文结论的影响、以及改进建议。

## 🗺️ 研究全景与影响
### 定位
这篇论文在研究时间线上处于什么位置？与最接近的先前工作对比如何？

### 学术脉络
本文建立在哪 2-3 个关键前序思想之上？

### 影响评估
对领域的可能影响（变革性/渐进式/小众）、下游应用、潜在社会影响。

### 未来方向
最有前景的下一步、一个高风险高回报的拓展、与当前研究趋势的关联。

## 📝 技术参考
### 关键概念与术语表
| 术语 | 定义 |
|------|------|
| （8-12 个领域专有术语或缩写） | （清晰简明的定义） |

### 关键公式与定理
最重要的数学公式及其通俗解释。

### 可复现性检查
- [ ] 代码是否公开？
- [ ] 数据是否公开？
- [ ] 超参数是否完整？
- [ ] 计算资源是否注明？
- [ ] 随机种子/方差是否报告？

---

用中文撰写。专有名词、模型名称、基准测试名保留英文原文。力求深入透彻而不冗长。

论文正文：
{paper_text}"""


# ══════════════════════════════════════════════════════
#  Internal LLM helpers
# ══════════════════════════════════════════════════════

def _stream_llm_sse(messages, model=None, max_tokens=4096, temperature=0):
    """Streaming SSE generator for paper Q&A / translate.

    Reuses dispatch_stream for retry handling and rate-limit rotation.
    Yields SSE-formatted lines including a final ``data: [DONE]\\n\\n``.
    """
    q = queue.Queue()
    _sentinel = object()

    def _worker():
        try:
            def _on_content(text):
                q.put(text)

            dispatch_stream(
                messages,
                on_content=_on_content,
                max_tokens=max_tokens,
                temperature=temperature,
                prefer_model=model or None,
                strict_model=bool(model),
                log_prefix='[Paper:Chat]',
            )
        except Exception as e:
            logger.error('[Paper:Chat] Stream failed: %s', e, exc_info=True)
            q.put(('__error__', str(e)))
        finally:
            q.put(_sentinel)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    while True:
        item = q.get()
        if item is _sentinel:
            break
        if isinstance(item, tuple) and item[0] == '__error__':
            yield f'data: {json.dumps({"error": item[1]})}\n\n'
            break
        yield f'data: {json.dumps({"choices": [{"delta": {"content": item}}]})}\n\n'

    yield 'data: [DONE]\n\n'


# ── Report tool definitions ──
_REPORT_TOOLS = [SEARCH_TOOL_MULTI, FETCH_URL_TOOL]
_MAX_REPORT_TOOL_ROUNDS = 8


def _execute_report_tool(name, args_str):
    """Execute a tool call from the report agent and return the result string.

    Supports web_search (single + batch) and fetch_url (single + batch).
    """
    try:
        args = json.loads(args_str) if args_str else {}
    except json.JSONDecodeError as e:
        logger.warning('[Paper:Report:Tool] Bad JSON args for %s: %s', name, e)
        return f'Error: invalid arguments JSON — {e}'

    if name == 'web_search':
        queries = args.get('queries', [])
        if not queries:
            q = args.get('query', '')
            if q:
                queries = [{'query': q}]
        if not queries:
            return 'Error: no query provided'
        all_results = []
        for qobj in queries[:5]:  # cap at 5 concurrent
            q = qobj.get('query', '') if isinstance(qobj, dict) else str(qobj)
            if not q:
                continue
            logger.info('[Paper:Report:Tool] web_search query=%r', q[:100])
            try:
                results = perform_web_search(q, max_results=5)
                for r in results:
                    snippet = (r.get('full_content') or r.get('snippet', ''))[:3000]
                    all_results.append(f"### {r.get('title', 'No title')}\nURL: {r.get('url', '')}\n{snippet}")
            except Exception as e:
                logger.warning('[Paper:Report:Tool] web_search failed for %r: %s', q, e)
                all_results.append(f'Search for "{q}" failed: {e}')
        return '\n\n---\n\n'.join(all_results) if all_results else 'No results found.'

    elif name == 'fetch_url':
        urls = args.get('urls', [])
        if not urls:
            u = args.get('url', '')
            if u:
                urls = [{'url': u}]
        if not urls:
            return 'Error: no url provided'
        all_contents = []
        for uobj in urls[:3]:  # cap at 3
            u = uobj.get('url', '') if isinstance(uobj, dict) else str(uobj)
            if not u:
                continue
            logger.info('[Paper:Report:Tool] fetch_url url=%.100s', u)
            try:
                content = fetch_page_content(u, max_chars=8000)
                all_contents.append(f"### Content from {u}\n{content[:8000]}")
            except Exception as e:
                logger.warning('[Paper:Report:Tool] fetch_url failed for %.100s: %s', u, e)
                all_contents.append(f'Fetch {u} failed: {e}')
        return '\n\n---\n\n'.join(all_contents) if all_contents else 'No content fetched.'

    else:
        return f'Unknown tool: {name}'


def _stream_report_with_tools(messages, model=None, temperature=0, abort_event=None):
    """Streaming report generator reusing dispatch_stream + tool loop.

    Yields SSE events via a thread-safe queue:
      - {"delta": str}       — content text chunk
      - {"thinking": str}    — reasoning/thinking text chunk (progress signal)
      - {"tool_call": {...}}  — tool being invoked
      - {"tool_done": {...}}  — tool finished
      - {"error": str}       — error

    Uses dispatch_stream (which wraps stream_chat) for proper retry handling,
    rate-limit rotation, and SSE parsing — no manual HTTP/SSE code here.
    """
    q = queue.Queue()
    _sentinel = object()

    def _abort_check():
        return abort_event.is_set() if abort_event else False

    def _worker():
        model_name = model or _lib.LLM_MODEL
        t0 = time.time()
        full_content = ''

        try:
            for rnd in range(_MAX_REPORT_TOOL_ROUNDS + 1):
                if _abort_check():
                    break

                # Stream one LLM round via dispatch_stream
                body = build_body(model_name, messages, temperature=temperature, stream=True)
                if rnd < _MAX_REPORT_TOOL_ROUNDS:
                    body['tools'] = _REPORT_TOOLS

                logger.info('[Paper:Report] Round %d — model=%s msgs=%d', rnd + 1, model_name, len(messages))

                def _on_content(text):
                    nonlocal full_content
                    full_content += text
                    q.put(('delta', text))

                def _on_thinking(text):
                    # Forward reasoning/thinking deltas so the UI can show
                    # progress *before* the model starts emitting content.
                    q.put(('thinking', text))

                msg, finish, usage = dispatch_stream(
                    body,
                    on_content=_on_content,
                    on_thinking=_on_thinking,
                    abort_check=_abort_check,
                    prefer_model=model_name if model else None,
                    strict_model=bool(model),
                    log_prefix='[Paper:Report]',
                )

                # No tool calls → done
                tool_calls = msg.get('tool_calls')
                if not tool_calls:
                    logger.info('[Paper:Report] Round %d — no tool calls, report complete '
                                '(%d chars, %.1fs)', rnd + 1, len(full_content), time.time() - t0)
                    break

                # Add assistant message with tool calls to history
                messages.append(msg)

                # Execute tool calls
                for tc in tool_calls:
                    fn_name = tc['function']['name']
                    fn_args = tc['function']['arguments']
                    tc_id = tc.get('id', '')

                    q.put(('tool_call', {'name': fn_name, 'id': tc_id}))

                    tool_t0 = time.time()
                    result = _execute_report_tool(fn_name, fn_args)
                    tool_elapsed = time.time() - tool_t0
                    logger.info('[Paper:Report:Tool] %s → %d chars in %.1fs', fn_name, len(result), tool_elapsed)

                    q.put(('tool_done', {'name': fn_name, 'id': tc_id, 'elapsed': round(tool_elapsed, 1)}))

                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tc_id,
                        'content': result[:30000],
                    })

            elapsed = time.time() - t0
            logger.info('[Paper:Report] Complete — %d chars in %.1fs', len(full_content), elapsed)

        except Exception as e:
            logger.error('[Paper:Report] Failed: %s', e, exc_info=True)
            q.put(('error', str(e)))
        finally:
            q.put(_sentinel)

    # Run in background thread so we can yield from the generator
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    while True:
        item = q.get()
        if item is _sentinel:
            break
        event_type, payload = item
        if event_type == 'delta':
            yield f'data: {json.dumps({"delta": payload})}\n\n'
        elif event_type == 'thinking':
            yield f'data: {json.dumps({"thinking": payload})}\n\n'
        elif event_type == 'tool_call':
            yield f'data: {json.dumps({"tool_call": payload})}\n\n'
        elif event_type == 'tool_done':
            yield f'data: {json.dumps({"tool_done": payload})}\n\n'
        elif event_type == 'error':
            yield f'data: {json.dumps({"error": payload})}\n\n'


# ══════════════════════════════════════════════════════
#  API Endpoints
# ══════════════════════════════════════════════════════

@paper_bp.route('/api/paper/chat', methods=['POST'])
def paper_chat():
    """Streaming LLM chat for paper Q&A / translation.

    Body JSON:
        messages: list — OpenAI-format messages [{role, content}, ...]
        model: str (optional) — LLM model to use
    Returns:
        SSE stream of chat completion deltas.
    """
    data = request.get_json(silent=True) or {}
    messages = data.get('messages', [])
    model = data.get('model') or None

    if not messages:
        logger.warning('[Paper:Chat] Request with no messages')
        return jsonify({'error': 'No messages provided'}), 400

    # Log the request (truncate user message for privacy)
    last_msg = messages[-1] if messages else {}
    last_content_preview = str(last_msg.get('content', ''))[:200]
    logger.info('[Paper:Chat] Request — %d messages, model=%s, last_msg_role=%s, preview=%.200s',
                len(messages), model, last_msg.get('role', '?'), last_content_preview)

    def generate():
        yield from _stream_llm_sse(messages, model=model)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@paper_bp.route('/api/paper/extract-images', methods=['POST'])
def extract_images():
    """Extract figure/table images from a previously uploaded PDF.

    Body JSON:
        filename: str — the filename returned by /api/paper/upload or /api/paper/fetch-arxiv
        paper_hash: str (optional) — if omitted, computed from filename bytes
        max_images: int (optional) — cap, default 30
        max_image_width: int (optional) — default 900

    Returns:
        { ok: true, paper_hash: str, images: [{url, caption, page, source, width, height}] }
    """
    data = request.get_json(silent=True) or {}
    filename = os.path.basename((data.get('filename') or '').strip())
    if not filename:
        logger.warning('[Paper:Images] Request with no filename')
        return jsonify({'error': 'No filename'}), 400

    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.isfile(filepath):
        logger.warning('[Paper:Images] PDF not found: %s', filename)
        return jsonify({'error': 'PDF not found'}), 404

    try:
        max_images = int(data.get('max_images', 30))
        max_image_width = int(data.get('max_image_width', 900))
    except (ValueError, TypeError) as e:
        logger.warning('[Paper:Images] Invalid numeric parameter: %s', e)
        return jsonify({'error': f'Invalid parameter: {e}'}), 400

    # Cache key — prefer client-provided hash (matches the report cache key),
    # fall back to filename-based hash.
    phash = _safe_hash_dir(data.get('paper_hash', '').strip()) or _paper_hash(filename)
    out_dir = os.path.join(PAPER_IMG_DIR, phash)

    # ── Cache hit: re-use previously extracted images ──
    manifest_path = os.path.join(out_dir, 'manifest.json')
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            logger.info('[Paper:Images] Cache hit — hash=%s, %d images', phash, len(cached))
            return jsonify({'ok': True, 'paper_hash': phash, 'images': cached, 'cached': True})
        except Exception as e:
            logger.warning('[Paper:Images] Manifest cache read failed (will regenerate): %s', e)

    # ── Extract ──
    try:
        import pymupdf
    except ImportError as e:
        logger.error('[Paper:Images] pymupdf not available: %s', e)
        return jsonify({'error': 'pymupdf not available on server'}), 500

    from lib.pdf_parser.images import detect_and_clip_figures

    t0 = time.time()
    try:
        with open(filepath, 'rb') as f:
            pdf_bytes = f.read()
    except Exception as e:
        logger.error('[Paper:Images] Read failed: %s', e, exc_info=True)
        return jsonify({'error': f'Read failed: {e}'}), 500

    os.makedirs(out_dir, exist_ok=True)
    images_out = []
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
        try:
            total_pages = len(doc)
            for pi in range(total_pages):
                if len(images_out) >= max_images:
                    break
                try:
                    page_imgs = detect_and_clip_figures(
                        doc[pi], pi, total_pages,
                        max_image_width=max_image_width,
                    )
                except Exception as pe:
                    logger.warning('[Paper:Images] detect failed on page %d: %s', pi, pe)
                    continue
                for img in page_imgs:
                    if len(images_out) >= max_images:
                        break
                    try:
                        import base64 as _b64
                        raw = _b64.b64decode(img['base64'])
                        idx = len(images_out) + 1
                        ext = '.jpg' if 'jpeg' in img.get('mediaType', '') else '.png'
                        fname = f'fig_{idx:02d}_p{img.get("page", pi+1)}{ext}'
                        fpath = os.path.join(out_dir, fname)
                        with open(fpath, 'wb') as f:
                            f.write(raw)
                        images_out.append({
                            'url': f'/api/paper/images/{phash}/{fname}',
                            'caption': img.get('caption', ''),
                            'page': img.get('page'),
                            'source': img.get('source', ''),
                            'width': img.get('width'),
                            'height': img.get('height'),
                        })
                    except Exception as se:
                        logger.warning('[Paper:Images] Failed to save figure %d on page %d: %s',
                                       len(images_out)+1, pi, se)
        finally:
            doc.close()
    except Exception as e:
        logger.error('[Paper:Images] Extraction failed: %s', e, exc_info=True)
        return jsonify({'error': f'Extraction failed: {e}'}), 500

    # Persist manifest for future cache hits
    try:
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(images_out, f, ensure_ascii=False)
    except Exception as e:
        logger.warning('[Paper:Images] Failed to write manifest: %s', e)

    elapsed = time.time() - t0
    logger.info('[Paper:Images] Extracted %d images from %s in %.1fs (hash=%s)',
                len(images_out), filename, elapsed, phash)
    return jsonify({'ok': True, 'paper_hash': phash, 'images': images_out})


@paper_bp.route('/api/paper/images/<phash>/<filename>')
def serve_paper_image(phash, filename):
    """Serve an extracted paper figure image."""
    phash_safe = _safe_hash_dir(phash)
    if not phash_safe:
        logger.debug('[Paper:Images] Invalid hash: %.40s', phash)
        return jsonify({'error': 'Invalid hash'}), 400
    filename = os.path.basename(filename)
    # Only allow our known filename pattern
    if not re.fullmatch(r'fig_\d+_p\d+\.(jpg|jpeg|png)', filename, re.IGNORECASE):
        logger.debug('[Paper:Images] Invalid filename: %s', filename)
        return jsonify({'error': 'Invalid filename'}), 400
    filepath = os.path.join(PAPER_IMG_DIR, phash_safe, filename)
    if not os.path.isfile(filepath):
        return jsonify({'error': 'Image not found'}), 404
    mt = 'image/jpeg' if filename.lower().endswith(('.jpg', '.jpeg')) else 'image/png'
    return send_file(filepath, mimetype=mt)


def _build_image_manifest(images, lang='en'):
    """Build a compact image manifest block for the LLM prompt."""
    if not images:
        return ''
    header = ('Image manifest — figures/tables extracted from the paper.\n'
              'Embed each as `![caption](url)` in Markdown where relevant.\n'
              'URLs must be copied VERBATIM from this list.\n') if lang != 'zh' else (
              '图像清单 —— 从论文中抽取的图/表。\n'
              '如需引用请在正文中用 `![说明](url)` 嵌入，URL 必须原样照抄。\n')
    lines = [header]
    for i, img in enumerate(images, 1):
        cap = (img.get('caption') or '').strip().replace('\n', ' ')[:160]
        page = img.get('page', '?')
        src = img.get('source', '')
        url = img.get('url', '')
        if not url:
            continue
        kind = 'table' if 'table' in src else 'figure'
        lines.append(f'{i}. [{kind} · p.{page}] {url}\n   caption: {cap}')
    return '\n'.join(lines)


@paper_bp.route('/api/paper/report', methods=['POST'])
def start_report():
    """Single-model streaming paper analysis report.

    Body JSON:
        paper_text: str — full text of the paper
        model: str (optional) — LLM model to use
        lang: str (optional) — 'zh' for Chinese prompt, else English. Default 'en'.
    Returns:
        SSE stream. Each event is JSON with either:
          - {delta: str}  — incremental text chunk
          - {done: true}  — report complete
          - {error: str}  — error occurred
        First event may be {cached: true, report: str} if found in DB.
    """
    data = request.get_json(silent=True) or {}
    paper_text = data.get('paper_text', '').strip()
    if not paper_text:
        logger.warning('[Paper:Report] Request with no paper_text')
        return jsonify({'error': 'No paper_text provided'}), 400
    if len(paper_text) < 100:
        logger.warning('[Paper:Report] Paper text too short: %d chars', len(paper_text))
        return jsonify({'error': 'Paper text too short (< 100 chars)'}), 400

    model = data.get('model') or None
    lang = data.get('lang', 'en') or 'en'
    force = data.get('force', False)
    raw_images = data.get('images') or []
    if not isinstance(raw_images, list):
        raw_images = []
    # Keep only entries that have a url; cap count to keep the prompt small
    images = [im for im in raw_images
              if isinstance(im, dict) and im.get('url')][:30]

    # Check DB cache first (unless force=True)
    phash = _paper_hash(paper_text)
    if not force:
        try:
            db = get_db()
            row = db.execute(
                "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
                (phash, lang),
            ).fetchone()
            if row and row['report']:
                logger.info('[Paper:Report] DB cache hit — hash=%s lang=%s %d chars',
                            phash, lang, len(row['report']))

                def cached_gen():
                    yield f'data: {json.dumps({"cached": True, "report": row["report"], "paper_hash": phash})}\n\n'
                    yield f'data: {json.dumps({"done": True})}\n\n'

                return Response(cached_gen(), mimetype='text/event-stream',
                                headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
        except Exception as e:
            logger.warning('[Paper:Report] DB cache lookup failed (will regenerate): %s', e)
    else:
        logger.info('[Paper:Report] Force regeneration — skipping cache, hash=%s', phash)

    # Build single comprehensive prompt with tool-use instruction
    prompt_template = _REPORT_PROMPT_ZH if lang == 'zh' else _REPORT_PROMPT_EN
    # Limit paper text to avoid exceeding context window
    max_text = 120000
    truncated_text = paper_text[:max_text]
    if len(paper_text) > max_text:
        logger.info('[Paper:Report] Truncating paper text from %d to %d chars', len(paper_text), max_text)

    # Append image manifest (if any) to the paper_text slot so the model sees
    # it in the same user message, immediately after the paper body.
    manifest = _build_image_manifest(images, lang=lang)
    if manifest:
        truncated_text = truncated_text + '\n\n---\n\n' + manifest
        logger.info('[Paper:Report] Injected image manifest — %d images, hash=%s', len(images), phash)

    # Use literal .replace() rather than .format() because the prompt templates
    # contain KaTeX examples with literal `{` `}` that would confuse .format().
    prompt = prompt_template.replace('{paper_text}', truncated_text)

    # Prepend tool-use instruction to system message
    tool_instruction = (
        "You have access to web_search and fetch_url tools. "
        "Use them to look up additional context when needed — for example, "
        "to find related work, verify claims, check the paper's impact/citations, "
        "or fill in details about referenced methods/datasets. "
        "You may call tools multiple times before writing the report. "
        "After gathering sufficient information, write the complete report.\n\n"
    )
    messages = [
        {'role': 'system', 'content': tool_instruction},
        {'role': 'user', 'content': prompt},
    ]

    logger.info('[Paper:Report] Starting tool-loop — model=%s lang=%s text_len=%d hash=%s',
                model, lang, len(paper_text), phash)

    def generate():
        full_text = ''
        t0 = time.time()
        try:
            for sse_line in _stream_report_with_tools(messages, model=model, temperature=0):
                # Pass through all events and track content for DB persistence
                if not sse_line.startswith('data: '):
                    continue
                payload = sse_line[6:].strip()
                if payload == '[DONE]':
                    continue
                try:
                    ev = json.loads(payload)
                    if ev.get('delta'):
                        full_text += ev['delta']
                except (json.JSONDecodeError, TypeError):
                    pass
                yield sse_line

            elapsed = time.time() - t0
            logger.info('[Paper:Report] Stream complete — %d chars in %.1fs hash=%s',
                        len(full_text), elapsed, phash)

            # Persist to DB
            if full_text:
                try:
                    db2 = get_thread_db()
                    db_execute_with_retry(
                        db2,
                        "INSERT OR REPLACE INTO paper_reports (paper_hash, lang, report, model, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (phash, lang, full_text, model or _lib.LLM_MODEL, int(time.time())),
                    )
                    logger.info('[Paper:Report] Persisted to DB — hash=%s lang=%s %d chars',
                                phash, lang, len(full_text))
                except Exception as e:
                    logger.warning('[Paper:Report] Failed to persist report: %s', e)

            yield f'data: {json.dumps({"done": True, "paper_hash": phash})}\n\n'

        except Exception as e:
            elapsed = time.time() - t0
            logger.error('[Paper:Report] Stream failed after %.1fs: %s', elapsed, e, exc_info=True)
            yield f'data: {json.dumps({"error": str(e)})}\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@paper_bp.route('/api/paper/report/cache', methods=['POST'])
def get_report_cache():
    """Lookup cached report by paper hash.

    Body JSON:
        paper_hash: str — precomputed hash (preferred, avoids re-sending full text)
        paper_text: str — full text of the paper (fallback, used to compute hash)
        lang: str (optional) — language. Default 'en'.
    Returns:
        { ok: true, report: str, paper_hash: str } or { ok: false }
    """
    data = request.get_json(silent=True) or {}
    phash = data.get('paper_hash', '').strip()
    lang = data.get('lang', 'en') or 'en'

    # Prefer pre-computed hash; fall back to computing from text
    if not phash:
        paper_text = data.get('paper_text', '').strip()
        if not paper_text:
            return jsonify({'ok': False, 'error': 'No paper_hash or paper_text'}), 400
        phash = _paper_hash(paper_text)

    try:
        db = get_db()
        row = db.execute(
            "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
            (phash, lang),
        ).fetchone()
        if row and row['report']:
            logger.debug('[Paper:Report:Cache] Hit — hash=%s lang=%s', phash, lang)
            return jsonify({'ok': True, 'report': row['report'], 'paper_hash': phash})
    except Exception as e:
        logger.warning('[Paper:Report:Cache] Lookup failed: %s', e)

    return jsonify({'ok': False})


# ══════════════════════════════════════════════════════
#  arXiv / Upload / Serve endpoints
# ══════════════════════════════════════════════════════

@paper_bp.route('/api/paper/fetch-arxiv', methods=['POST'])
def fetch_arxiv():
    """Download PDF from arXiv URL and serve it locally.

    Body JSON:
        url: str — arXiv URL (abs page, pdf link, or just the ID like 2301.12345)
    Returns:
        { ok: true, pdf_url: str, title: str, arxiv_id: str }
    """
    data = request.get_json(silent=True) or {}
    url_input = data.get('url', '').strip()
    if not url_input:
        logger.warning('[Paper:arXiv] Fetch request with no URL')
        return jsonify({'error': 'No URL provided'}), 400

    arxiv_id = _extract_arxiv_id(url_input)
    if not arxiv_id:
        logger.warning('[Paper:arXiv] Could not parse arXiv ID from: %.200s', url_input)
        return jsonify({'error': 'Could not parse arXiv ID from URL'}), 400

    pdf_url = f'https://arxiv.org/pdf/{arxiv_id}.pdf'
    filename = f'arxiv_{arxiv_id.replace("/", "_")}.pdf'
    filepath = os.path.join(PAPER_DIR, filename)

    if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
        file_size = os.path.getsize(filepath)
        logger.info('[Paper:arXiv] Cache hit for %s — %d bytes at %s', arxiv_id, file_size, filepath)
        return jsonify({
            'ok': True,
            'pdf_url': f'/api/paper/pdf/{filename}',
            'arxiv_id': arxiv_id,
            'cached': True,
        })

    try:
        logger.info('[Paper:arXiv] Downloading PDF: %s', pdf_url)
        t0 = time.time()
        resp = _requests.get(pdf_url, timeout=60, stream=True,
                             headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '')
        if 'pdf' not in content_type and 'octet-stream' not in content_type:
            logger.warning('[Paper:arXiv] Unexpected content type: %s for %s', content_type, pdf_url)

        with open(filepath, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        file_size = os.path.getsize(filepath)
        elapsed = time.time() - t0
        logger.info('[Paper:arXiv] Downloaded %s: %d bytes in %.1fs', arxiv_id, file_size, elapsed)

        return jsonify({
            'ok': True,
            'pdf_url': f'/api/paper/pdf/{filename}',
            'arxiv_id': arxiv_id,
            'file_size': file_size,
        })

    except _requests.Timeout:
        logger.warning('[Paper:arXiv] Download timeout (60s): %s', pdf_url)
        return jsonify({'error': 'Download timed out (60s)'}), 504
    except _requests.RequestException as e:
        logger.warning('[Paper:arXiv] Download failed: %s — %s', pdf_url, e)
        return jsonify({'error': f'Download failed: {str(e)}'}), 502


@paper_bp.route('/api/paper/fetch-arxiv-stream', methods=['POST'])
def fetch_arxiv_stream():
    """Download PDF from arXiv and parse it — SSE stream of progress events.

    Body JSON:
        url: str — arXiv URL or ID

    SSE events (each one JSON on a ``data:`` line):
        {stage: 'resolve', arxiv_id: str, pdf_url: str}  — URL parsed
        {stage: 'download', downloaded: int, total: int}  — download progress
        {stage: 'download_done', file_size: int, elapsed: float}
        {stage: 'parse_start'}
        {stage: 'parse_done', total_pages: int, text_length: int, elapsed: float}
        {stage: 'done', ok: true, pdf_url: str, arxiv_id: str,
               parsed_text: str, total_pages: int, text_length: int, cached: bool}
        {stage: 'error', error: str}
    """
    data = request.get_json(silent=True) or {}
    url_input = (data.get('url') or '').strip()
    if not url_input:
        logger.warning('[Paper:arXiv:Stream] Fetch request with no URL')
        return jsonify({'error': 'No URL provided'}), 400

    arxiv_id = _extract_arxiv_id(url_input)
    if not arxiv_id:
        logger.warning('[Paper:arXiv:Stream] Could not parse arXiv ID from: %.200s', url_input)
        return jsonify({'error': 'Could not parse arXiv ID from URL'}), 400

    pdf_url = f'https://arxiv.org/pdf/{arxiv_id}.pdf'
    filename = f'arxiv_{arxiv_id.replace("/", "_")}.pdf'
    filepath = os.path.join(PAPER_DIR, filename)

    def _sse(obj):
        return f'data: {json.dumps(obj)}\n\n'

    def generate():
        yield _sse({'stage': 'resolve', 'arxiv_id': arxiv_id,
                    'pdf_url': f'/api/paper/pdf/{filename}'})

        # ── Step 1: Download PDF (cached or fresh) ──
        pdf_bytes = None
        cached = False
        try:
            if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
                cached = True
                with open(filepath, 'rb') as f:
                    pdf_bytes = f.read()
                file_size = len(pdf_bytes)
                logger.info('[Paper:arXiv:Stream] Cache hit for %s — %d bytes', arxiv_id, file_size)
                yield _sse({'stage': 'download_done', 'file_size': file_size,
                            'elapsed': 0.0, 'cached': True})
            else:
                logger.info('[Paper:arXiv:Stream] Downloading PDF: %s', pdf_url)
                t0 = time.time()
                resp = _requests.get(pdf_url, timeout=60, stream=True,
                                     headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
                resp.raise_for_status()
                content_type = resp.headers.get('Content-Type', '')
                if 'pdf' not in content_type and 'octet-stream' not in content_type:
                    logger.warning('[Paper:arXiv:Stream] Unexpected content type: %s for %s',
                                   content_type, pdf_url)

                total = 0
                try:
                    total = int(resp.headers.get('Content-Length') or 0)
                except (ValueError, TypeError) as e:
                    logger.debug('[Paper:arXiv:Stream] Bad Content-Length: %s', e)

                downloaded = 0
                last_progress_ts = 0.0
                chunks = []
                with open(filepath, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=32768):
                        if not chunk:
                            continue
                        f.write(chunk)
                        chunks.append(chunk)
                        downloaded += len(chunk)
                        # Emit at most ~10 progress events per second
                        now = time.time()
                        if now - last_progress_ts >= 0.1:
                            last_progress_ts = now
                            yield _sse({'stage': 'download',
                                        'downloaded': downloaded,
                                        'total': total})
                pdf_bytes = b''.join(chunks)
                file_size = len(pdf_bytes)
                elapsed = time.time() - t0
                logger.info('[Paper:arXiv:Stream] Downloaded %s: %d bytes in %.1fs',
                            arxiv_id, file_size, elapsed)
                yield _sse({'stage': 'download_done', 'file_size': file_size,
                            'elapsed': round(elapsed, 2), 'cached': False})
        except _requests.Timeout:
            logger.warning('[Paper:arXiv:Stream] Download timeout (60s): %s', pdf_url)
            yield _sse({'stage': 'error', 'error': 'Download timed out (60s)'})
            return
        except _requests.RequestException as e:
            logger.warning('[Paper:arXiv:Stream] Download failed: %s — %s', pdf_url, e)
            yield _sse({'stage': 'error', 'error': f'Download failed: {e}'})
            return
        except OSError as e:
            logger.error('[Paper:arXiv:Stream] Disk write failed for %s: %s',
                         filepath, e, exc_info=True)
            yield _sse({'stage': 'error', 'error': f'Disk write failed: {e}'})
            return

        # ── Step 2: Parse PDF text on server (no second client round-trip) ──
        if not pdf_bytes:
            logger.warning('[Paper:arXiv:Stream] No PDF bytes after download for %s', arxiv_id)
            yield _sse({'stage': 'error', 'error': 'PDF body was empty after download'})
            return

        yield _sse({'stage': 'parse_start'})
        try:
            from lib.pdf_parser import parse_pdf as _parse_pdf
            t0 = time.time()
            result = _parse_pdf(pdf_bytes, max_text_chars=0, max_images=0)
            elapsed = time.time() - t0
            parsed_text = result.get('text') or ''
            total_pages = result.get('totalPages', 0)
            text_length = result.get('textLength', len(parsed_text))
            logger.info('[Paper:arXiv:Stream] Parsed %s — %d pages, %d chars in %.1fs',
                        arxiv_id, total_pages, text_length, elapsed)
            yield _sse({'stage': 'parse_done',
                        'total_pages': total_pages,
                        'text_length': text_length,
                        'elapsed': round(elapsed, 2)})
        except Exception as e:
            # Parsing failed — still return the PDF URL so the viewer can render it,
            # but surface the error so the UI can warn the user.
            logger.error('[Paper:arXiv:Stream] PDF parse failed for %s: %s',
                         arxiv_id, e, exc_info=True)
            yield _sse({'stage': 'done', 'ok': True,
                        'pdf_url': f'/api/paper/pdf/{filename}',
                        'arxiv_id': arxiv_id,
                        'parsed_text': '',
                        'total_pages': 0,
                        'text_length': 0,
                        'cached': cached,
                        'parse_error': f'PDF parse failed: {e}'})
            return

        # ── Done — return everything the client needs ──
        yield _sse({'stage': 'done', 'ok': True,
                    'pdf_url': f'/api/paper/pdf/{filename}',
                    'arxiv_id': arxiv_id,
                    'parsed_text': parsed_text,
                    'total_pages': total_pages,
                    'text_length': text_length,
                    'cached': cached})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@paper_bp.route('/api/paper/pdf/<filename>')
def serve_paper_pdf(filename):
    """Serve a downloaded paper PDF."""
    filename = os.path.basename(filename)
    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.exists(filepath):
        logger.debug('[Paper] PDF not found: %s', filename)
        return jsonify({'error': 'PDF not found'}), 404
    return send_file(filepath, mimetype='application/pdf')


@paper_bp.route('/api/paper/upload', methods=['POST'])
def upload_paper():
    """Upload a PDF file for paper reading mode.

    Returns:
        { ok: true, pdf_url: str, filename: str }
    """
    if 'file' not in request.files:
        logger.warning('[Paper:Upload] No file in request')
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if not file.filename:
        logger.warning('[Paper:Upload] Empty filename')
        return jsonify({'error': 'No filename'}), 400
    if not file.filename.lower().endswith('.pdf'):
        logger.warning('[Paper:Upload] Non-PDF file rejected: %s', file.filename)
        return jsonify({'error': 'Only PDF files are supported'}), 400

    original_name = file.filename
    filename = f"{int(time.time() * 1000)}_{original_name}"
    filename = re.sub(r'[^\w\-.]', '_', filename)
    filepath = os.path.join(PAPER_DIR, filename)

    try:
        file.save(filepath)
        file_size = os.path.getsize(filepath)
        logger.info('[Paper:Upload] Saved: %s (%d bytes) — original=%s', filename, file_size, original_name)
        return jsonify({
            'ok': True,
            'pdf_url': f'/api/paper/pdf/{filename}',
            'filename': filename,
            'file_size': file_size,
        })
    except Exception as e:
        logger.error('[Paper:Upload] Failed to save %s: %s', filename, e, exc_info=True)
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500


def _extract_arxiv_id(url_or_id):
    """Extract arXiv paper ID from various URL formats.

    Supports:
        - 2301.12345
        - 2301.12345v2
        - arxiv.org/abs/2301.12345
        - arxiv.org/pdf/2301.12345
        - arxiv.org/pdf/2301.12345.pdf
        - arxiv.org/abs/hep-th/0601001
        - https://arxiv.org/abs/2301.12345
    """
    url_or_id = url_or_id.strip()

    m = re.match(r'^(\d{4}\.\d{4,5})(v\d+)?$', url_or_id)
    if m:
        return m.group(1) + (m.group(2) or '')

    m = re.match(r'^([a-z-]+/\d{7})(v\d+)?$', url_or_id)
    if m:
        return m.group(1) + (m.group(2) or '')

    m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)', url_or_id)
    if m:
        return m.group(1)

    m = re.search(r'arxiv\.org/(?:abs|pdf)/([a-z-]+/\d{7}(?:v\d+)?)', url_or_id)
    if m:
        return m.group(1)

    return None
