from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from pathlib import Path
import asyncio
import json
import os
import time
import uuid
import httpx

app = FastAPI()
BASE_DIR = Path(__file__).parent
jobs: dict = {}
BATCH_HISTORY_FILE = BASE_DIR / "batch_history.json"


def load_batch_history() -> list:
    try:
        if BATCH_HISTORY_FILE.exists():
            return json.loads(BATCH_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def save_batch_history(batch_id: str, created_at: str, total: int):
    history = [h for h in load_batch_history() if h.get("batch_id") != batch_id]
    history.insert(0, {"batch_id": batch_id, "created_at": created_at, "total": total})
    try:
        BATCH_HISTORY_FILE.write_text(json.dumps(history[:2], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


@app.get("/")
def index():
    return HTMLResponse((BASE_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/browse")
def browse():
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", True)
    path = filedialog.askdirectory(title="選擇資料夾")
    root.destroy()
    return {"path": path}


@app.get("/browse-file")
def browse_file():
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="選擇提示詞檔案",
        filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
    )
    root.destroy()
    if not path:
        return {"path": "", "content": ""}
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    return {"path": path, "content": content}


@app.get("/models")
async def get_models():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"models": [], "error": "未設定 ANTHROPIC_API_KEY"}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.anthropic.com/v1/models", headers=headers)
        if resp.status_code != 200:
            return {"models": [], "error": f"HTTP {resp.status_code}"}
        data = resp.json()
    except Exception as e:
        return {"models": [], "error": str(e)}

    models = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if "image" in mid.lower():
            continue
        name = m.get("display_name", mid)
        if "opus" in mid:
            tier = "powerful"
        elif "sonnet" in mid:
            tier = "balanced"
        elif "haiku" in mid:
            tier = "fast"
        else:
            tier = ""
        models.append({"id": mid, "name": name, "tier": tier})
    return {"models": models}


class StartRequest(BaseModel):
    input_folder: str
    output_folder: str
    prompt: str
    model: str
    token_threshold: int  # 0 = unlimited
    mode: str = "standard"  # "standard" or "batch"


@app.post("/start")
async def start_job(req: StartRequest):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "request": req,
        "resume_event": asyncio.Event(),
        "stop_event": asyncio.Event(),
    }
    return {"job_id": job_id}


@app.post("/resume/{job_id}")
async def resume_job(job_id: str):
    if job_id in jobs:
        jobs[job_id]["resume_event"].set()
    return {"ok": True}


@app.post("/stop/{job_id}")
async def stop_job(job_id: str):
    if job_id in jobs:
        jobs[job_id]["stop_event"].set()
        jobs[job_id]["resume_event"].set()
    return {"ok": True}


@app.get("/batch-status/{batch_id}")
async def batch_status(batch_id: str):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"error": "未設定 ANTHROPIC_API_KEY"}
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        batch = await client.messages.batches.retrieve(batch_id)
    except Exception as e:
        return {"error": str(e)[:200]}
    rc = batch.request_counts
    total  = rc.processing + rc.succeeded + rc.errored + rc.expired + rc.canceled
    done   = rc.succeeded + rc.errored + rc.expired + rc.canceled
    failed = rc.errored + rc.expired + rc.canceled
    pct    = round(done / total * 100, 1) if total > 0 else 0
    created_str = None
    if batch.created_at:
        try:
            dt = batch.created_at
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            created_str = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            created_str = str(batch.created_at)[:16]
    status_map = {"in_progress": "處理中", "ended": "已完成", "canceling": "取消中"}
    return {
        "batch_id": batch.id,
        "status": status_map.get(batch.processing_status, batch.processing_status),
        "total": total,
        "succeeded": rc.succeeded,
        "failed": failed,
        "processing": rc.processing,
        "done": done,
        "pct": pct,
        "created_at": created_str,
    }


@app.get("/batch-history")
def get_batch_history():
    return load_batch_history()


@app.get("/download-batch/{batch_id}")
async def download_batch_results(batch_id: str, output_folder: str, input_folder: str = ""):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    async def generate():
        if not api_key:
            yield f"data: {json.dumps({'type': 'error', 'message': '未設定 ANTHROPIC_API_KEY'})}\n\n"
            return

        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)

        output_path = Path(output_folder.strip())
        try:
            output_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'無法建立輸出資料夾：{e}'})}\n\n"
            return

        # 優先讀取提交時儲存的檔名對應
        custom_id_to_name: dict[str, str] | None = None
        map_file = BASE_DIR / f"{batch_id}.map.json"
        if map_file.exists():
            try:
                custom_id_to_name = json.loads(map_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # 若對應檔不存在，嘗試從 input_folder 重建
        if custom_id_to_name is None:
            if not input_folder.strip():
                yield f"data: {json.dumps({'type': 'error', 'message': '找不到批次檔名對應記錄，請填寫輸入資料夾以重建對應'})}\n\n"
                return
            input_path = Path(input_folder.strip())
            if not input_path.is_dir():
                yield f"data: {json.dumps({'type': 'error', 'message': f'找不到輸入資料夾：{input_folder}'})}\n\n"
                return
            txt_files = sorted(f for f in input_path.iterdir() if f.is_file() and f.suffix.lower() == ".txt")
            custom_id_to_name = {f"file_{i:04d}": f.name for i, f in enumerate(txt_files)}

        total = len(custom_id_to_name)

        try:
            batch = await client.messages.batches.retrieve(batch_id)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'批次查詢失敗：{str(e)[:200]}'})}\n\n"
            return

        if batch.processing_status != "ended":
            status_map = {"in_progress": "處理中", "canceling": "取消中"}
            status = status_map.get(batch.processing_status, batch.processing_status)
            yield f"data: {json.dumps({'type': 'error', 'message': f'批次尚未完成，目前狀態：{status}'})}\n\n"
            return

        succeeded = 0
        failed = 0
        failed_files = []
        total_tokens = 0
        t0 = time.time()

        try:
            async for result in await client.messages.batches.results(batch_id):
                filename = custom_id_to_name.get(result.custom_id)
                if not filename:
                    continue
                if result.result.type == "succeeded":
                    msg    = result.result.message
                    text   = msg.content[0].text if msg.content else ""
                    tokens = (msg.usage.input_tokens or 0) + (msg.usage.output_tokens or 0)
                    total_tokens += tokens
                    try:
                        (output_path / filename).write_text(text, encoding="utf-8")
                        succeeded += 1
                        yield f"data: {json.dumps({'type': 'batch_file_result', 'filename': filename, 'tokens': tokens, 'success': True})}\n\n"
                    except Exception as e:
                        failed += 1
                        failed_files.append({"filename": filename, "error": f"寫入失敗：{e}"})
                        yield f"data: {json.dumps({'type': 'batch_file_result', 'filename': filename, 'tokens': tokens, 'success': False, 'error': f'寫入失敗：{e}'})}\n\n"
                else:
                    err_type  = result.result.type
                    error_msg = getattr(result.result, "error", None)
                    error_msg = getattr(error_msg, "message", str(error_msg)) if error_msg else \
                                {"expired": "已逾期", "canceled": "已取消"}.get(err_type, err_type)
                    failed += 1
                    failed_files.append({"filename": filename, "error": error_msg})
                    yield f"data: {json.dumps({'type': 'batch_file_result', 'filename': filename, 'tokens': 0, 'success': False, 'error': error_msg})}\n\n"
                await asyncio.sleep(0)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'下載結果失敗：{str(e)[:200]}'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'batch_done', 'total': total, 'succeeded': succeeded, 'failed': failed, 'total_tokens': total_tokens, 'elapsed': round(time.time() - t0, 1), 'failed_files': failed_files})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 標準模式 ────────────────────────────────────────────────────────────────

async def run_standard(job_id: str, queue: asyncio.Queue):
    job = jobs.get(job_id)
    if not job:
        await queue.put(None)
        return

    req = job["request"]
    resume_event: asyncio.Event = job["resume_event"]
    stop_event: asyncio.Event = job["stop_event"]
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        await queue.put({"type": "error", "message": "未設定 ANTHROPIC_API_KEY 環境變數"})
        await queue.put(None)
        return

    input_folder = Path(req.input_folder.strip())
    output_folder = Path(req.output_folder.strip())

    if not input_folder.is_dir():
        await queue.put({"type": "error", "message": f"找不到輸入資料夾：{input_folder}"})
        await queue.put(None)
        return

    try:
        output_folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        await queue.put({"type": "error", "message": f"無法建立輸出資料夾：{e}"})
        await queue.put(None)
        return

    txt_files = sorted(
        f for f in input_folder.iterdir()
        if f.is_file() and f.suffix.lower() == ".txt"
    )
    if not txt_files:
        await queue.put({"type": "error", "message": "輸入資料夾內沒有 .txt 檔案"})
        await queue.put(None)
        return

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)

    total = len(txt_files)
    cumulative_tokens = 0
    succeeded = 0
    failed = 0
    failed_files = []
    warned = False
    batch_start = time.time()

    for i, txt_file in enumerate(txt_files):
        if stop_event.is_set():
            await queue.put({
                "type": "stopped",
                "completed": [f.name for f in txt_files[:i]],
                "remaining": [f.name for f in txt_files[i:]],
            })
            await queue.put(None)
            return

        await queue.put({"type": "file_start", "index": i + 1, "total": total, "filename": txt_file.name})
        file_start = time.time()

        try:
            content = txt_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            failed += 1
            failed_files.append({"filename": txt_file.name, "error": str(e)})
            await queue.put({
                "type": "file_done", "index": i + 1, "total": total, "filename": txt_file.name,
                "tokens": 0, "elapsed": round(time.time() - file_start, 1),
                "success": False, "error": str(e),
            })
            continue

        full_prompt = f"{req.prompt}\n\n{content}"
        result_text = None
        file_tokens = 0
        file_error = None

        for attempt in range(3):
            try:
                response = await client.messages.create(
                    model=req.model,
                    max_tokens=8192,
                    messages=[{"role": "user", "content": full_prompt}],
                )
                if not response.content:
                    file_error = "API 回傳空回應"
                    break
                result_text = response.content[0].text
                file_tokens = (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0)
                break
            except anthropic.RateLimitError as e:
                err_str = str(e)
                if any(kw in err_str.lower() for kw in ["daily", "quota", "exceeded"]):
                    await queue.put({
                        "type": "quota_exceeded",
                        "stopped_at": i + 1,
                        "total": total,
                        "completed": [f.name for f in txt_files[:i]],
                        "remaining": [f.name for f in txt_files[i:]],
                    })
                    await queue.put(None)
                    return
                if attempt < 2:
                    await queue.put({"type": "retry", "index": i + 1, "attempt": attempt + 1, "filename": txt_file.name})
                    await asyncio.sleep(60)
                else:
                    file_error = "達到速率限制，重試 3 次後仍失敗"
            except anthropic.BadRequestError as e:
                err_str = str(e)
                if "too long" in err_str.lower() or "context" in err_str.lower():
                    file_error = "超過模型 context window"
                else:
                    file_error = err_str[:200]
                break
            except Exception as e:
                file_error = str(e)[:200]
                break

        elapsed = round(time.time() - file_start, 1)

        if result_text is not None:
            try:
                out_path = output_folder / txt_file.name
                out_path.write_text(result_text, encoding="utf-8")
            except Exception as e:
                failed += 1
                failed_files.append({"filename": txt_file.name, "error": f"寫入失敗：{e}"})
                await queue.put({
                    "type": "file_done", "index": i + 1, "total": total, "filename": txt_file.name,
                    "tokens": file_tokens, "elapsed": elapsed, "success": False, "error": f"寫入失敗：{e}",
                })
                continue

            succeeded += 1
            cumulative_tokens += file_tokens
            await queue.put({
                "type": "file_done", "index": i + 1, "total": total, "filename": txt_file.name,
                "tokens": file_tokens, "elapsed": elapsed, "success": True,
                "cumulative_tokens": cumulative_tokens,
            })

            if req.token_threshold > 0 and not warned and cumulative_tokens >= req.token_threshold:
                warned = True
                resume_event.clear()
                await queue.put({
                    "type": "token_warning",
                    "cumulative_tokens": cumulative_tokens,
                    "threshold": req.token_threshold,
                })
                await resume_event.wait()
                if stop_event.is_set():
                    await queue.put({
                        "type": "stopped",
                        "completed": [f.name for f in txt_files[:i + 1]],
                        "remaining": [f.name for f in txt_files[i + 1:]],
                    })
                    await queue.put(None)
                    return
        else:
            failed += 1
            failed_files.append({"filename": txt_file.name, "error": file_error})
            await queue.put({
                "type": "file_done", "index": i + 1, "total": total, "filename": txt_file.name,
                "tokens": 0, "elapsed": elapsed, "success": False, "error": file_error,
            })

    await queue.put({
        "type": "batch_done",
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "total_tokens": cumulative_tokens,
        "elapsed": round(time.time() - batch_start, 1),
        "failed_files": failed_files,
    })
    await queue.put(None)


# ── 批次 API 模式 ────────────────────────────────────────────────────────────

async def run_batch_api(job_id: str, queue: asyncio.Queue):
    job = jobs.get(job_id)
    if not job:
        await queue.put(None)
        return

    req = job["request"]
    stop_event: asyncio.Event = job["stop_event"]
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        await queue.put({"type": "error", "message": "未設定 ANTHROPIC_API_KEY 環境變數"})
        await queue.put(None)
        return

    input_folder = Path(req.input_folder.strip())
    output_folder = Path(req.output_folder.strip())

    if not input_folder.is_dir():
        await queue.put({"type": "error", "message": f"找不到輸入資料夾：{input_folder}"})
        await queue.put(None)
        return

    try:
        output_folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        await queue.put({"type": "error", "message": f"無法建立輸出資料夾：{e}"})
        await queue.put(None)
        return

    txt_files = sorted(
        f for f in input_folder.iterdir()
        if f.is_file() and f.suffix.lower() == ".txt"
    )
    if not txt_files:
        await queue.put({"type": "error", "message": "輸入資料夾內沒有 .txt 檔案"})
        await queue.put(None)
        return

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)

    # 讀取所有檔案並建立批次請求
    custom_id_map: dict[str, Path] = {}
    batch_requests = []
    pre_failed = []

    for i, txt_file in enumerate(txt_files):
        try:
            content = txt_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            pre_failed.append({"filename": txt_file.name, "error": str(e)})
            continue
        custom_id = f"file_{i:04d}"
        custom_id_map[custom_id] = txt_file
        batch_requests.append({
            "custom_id": custom_id,
            "params": {
                "model": req.model,
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": f"{req.prompt}\n\n{content}"}],
            },
        })

    if not batch_requests:
        await queue.put({"type": "error", "message": "所有檔案讀取失敗，無法提交批次"})
        await queue.put(None)
        return

    # 提交批次
    try:
        batch = await client.messages.batches.create(requests=batch_requests)
    except Exception as e:
        await queue.put({"type": "error", "message": f"批次提交失敗：{e}"})
        await queue.put(None)
        return

    total = len(txt_files)
    batch_start = time.time()

    _created_str = time.strftime("%Y-%m-%d %H:%M")
    try:
        from datetime import timezone as _tz
        _dt = batch.created_at
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=_tz.utc)
        _created_str = _dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    save_batch_history(batch.id, _created_str, total)
    try:
        _map = {cid: f.name for cid, f in custom_id_map.items()}
        (BASE_DIR / f"{batch.id}.map.json").write_text(
            json.dumps(_map, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass

    await queue.put({
        "type": "batch_submitted",
        "batch_id": batch.id,
        "total": total,
        "submitted": len(batch_requests),
        "pre_failed": len(pre_failed),
    })

    # 輪詢直到批次結束（每 5 秒檢查一次 stop_event，每 30 秒查詢 API）
    while batch.processing_status == "in_progress":
        for _ in range(6):
            if stop_event.is_set():
                try:
                    await client.messages.batches.cancel(batch.id)
                except Exception:
                    pass
                await queue.put({
                    "type": "stopped",
                    "completed": [],
                    "remaining": [f.name for f in txt_files],
                    "batch_id": batch.id,
                })
                await queue.put(None)
                return
            await asyncio.sleep(5)

        try:
            batch = await client.messages.batches.retrieve(batch.id)
        except Exception as e:
            await queue.put({"type": "error", "message": f"批次查詢失敗：{e}"})
            await queue.put(None)
            return

        rc = batch.request_counts
        await queue.put({
            "type": "batch_polling",
            "batch_id": batch.id,
            "processing_status": batch.processing_status,
            "request_counts": {
                "processing": rc.processing,
                "succeeded": rc.succeeded,
                "errored": rc.errored,
                "expired": rc.expired,
                "canceled": rc.canceled,
            },
        })

    if batch.processing_status != "ended":
        await queue.put({"type": "error", "message": f"批次異常結束，狀態：{batch.processing_status}"})
        await queue.put(None)
        return

    # 下載並處理結果
    await queue.put({"type": "batch_downloading", "batch_id": batch.id})

    succeeded = 0
    failed = len(pre_failed)
    failed_files = list(pre_failed)
    total_tokens = 0

    try:
        async for result in await client.messages.batches.results(batch.id):
            txt_file = custom_id_map.get(result.custom_id)
            if not txt_file:
                continue

            if result.result.type == "succeeded":
                msg = result.result.message
                text = msg.content[0].text if msg.content else ""
                tokens = (msg.usage.input_tokens or 0) + (msg.usage.output_tokens or 0)
                total_tokens += tokens
                try:
                    (output_folder / txt_file.name).write_text(text, encoding="utf-8")
                    succeeded += 1
                    await queue.put({
                        "type": "batch_file_result",
                        "filename": txt_file.name,
                        "tokens": tokens,
                        "success": True,
                    })
                except Exception as e:
                    failed += 1
                    failed_files.append({"filename": txt_file.name, "error": f"寫入失敗：{e}"})
                    await queue.put({
                        "type": "batch_file_result",
                        "filename": txt_file.name,
                        "tokens": tokens,
                        "success": False,
                        "error": f"寫入失敗：{e}",
                    })
            else:
                err_type = result.result.type
                if err_type == "errored":
                    err_obj = result.result.error
                    error_msg = getattr(err_obj, "message", str(err_obj))
                else:
                    error_msg = {"expired": "已逾期", "canceled": "已取消"}.get(err_type, err_type)
                failed += 1
                failed_files.append({"filename": txt_file.name, "error": error_msg})
                await queue.put({
                    "type": "batch_file_result",
                    "filename": txt_file.name,
                    "tokens": 0,
                    "success": False,
                    "error": error_msg,
                })
    except Exception as e:
        await queue.put({"type": "error", "message": f"下載結果失敗：{e}"})
        await queue.put(None)
        return

    await queue.put({
        "type": "batch_done",
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "total_tokens": total_tokens,
        "elapsed": round(time.time() - batch_start, 1),
        "failed_files": failed_files,
    })
    await queue.put(None)


# ── 分派與 SSE ───────────────────────────────────────────────────────────────

async def run_job(job_id: str, queue: asyncio.Queue):
    job = jobs.get(job_id)
    if not job:
        await queue.put(None)
        return
    if job["request"].mode == "batch":
        await run_batch_api(job_id, queue)
    else:
        await run_standard(job_id, queue)


@app.get("/stream/{job_id}")
async def stream_job(job_id: str):
    if job_id not in jobs:
        async def err():
            yield f"data: {json.dumps({'type': 'error', 'message': '找不到工作'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    q: asyncio.Queue = asyncio.Queue()

    async def generate():
        task = asyncio.create_task(run_job(job_id, q))
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=20.0)
                    if item is None:
                        break
                    yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            task.cancel()
            jobs.pop(job_id, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
