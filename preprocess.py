#!/usr/bin/env python3
"""
音频批量预处理：VAD断句 + Qwen3.5-Omni ASR预标注
500小时音频优化版 — 多线程并发ASR，断点续传，不保存中间音频文件
"""

import argparse, json, os, sys, time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from db import db_conn
from preprocess_store import ingestion_states, load_draft_segments, store_preprocessed_task

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".webm", ".opus", ".wma", ".aac"}


class AsrInterrupted(RuntimeError):
    pass


class AsrArrearage(AsrInterrupted):
    """DashScope account is overdue; stop the whole batch immediately."""


# ============================================================
# 配置
# ============================================================
def load_config():
    c = {
        "audio_dir": "./audio", "annotations_dir": "./annotations", "vad": {"min_speech_duration_ms": 3000,
        "min_silence_duration_ms": 300, "threshold": 0.5, "max_segment_duration_s": 30,
        "speech_pad_ms": 100}, "asr": {"api_key": "", "model": "qwen3.5-omni-plus",
        "language": "ar", "workers": 10},
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            for k in loaded:
                if k in c and isinstance(c[k], dict) and isinstance(loaded[k], dict):
                    c[k].update(loaded[k])
                else:
                    c[k] = loaded[k]
    return c


# ============================================================
# VAD 模块
# ============================================================
_vad = None

def get_vad():
    global _vad
    if _vad is None:
        from silero_vad import load_silero_vad, get_speech_timestamps
        model = load_silero_vad()
        _vad = (model, get_speech_timestamps)
    return _vad


def run_vad(audio_path, vad_cfg):
    import soundfile as sf
    import torch
    model, get_ts = get_vad()
    wav, orig_sr = sf.read(str(audio_path), dtype="float32")
    if wav.ndim > 1: wav = wav.mean(axis=1)
    peak = abs(wav).max()
    if peak > 0: wav = wav / peak * 0.9
    target_sr = 16000
    wav_t = torch.from_numpy(wav).unsqueeze(0)
    if orig_sr != target_sr:
        import torchaudio.transforms as T
        wav_t = T.Resample(orig_sr, target_sr)(wav_t)
    wav_mono = wav_t.squeeze()
    segs = get_ts(wav_mono, model, threshold=vad_cfg.get("threshold", 0.5),
        sampling_rate=target_sr, min_speech_duration_ms=vad_cfg.get("min_speech_duration_ms", 3000),
        min_silence_duration_ms=vad_cfg.get("min_silence_duration_ms", 300),
        speech_pad_ms=vad_cfg.get("speech_pad_ms", 100),
        max_speech_duration_s=vad_cfg.get("max_segment_duration_s", 30))
    return [{"id": i+1, "start": s["start"]/target_sr, "end": s["end"]/target_sr,
             "duration": (s["end"]-s["start"])/target_sr, "asr_text": "", "text": "",
             "exclude_from_training": False}
            for i, s in enumerate(segs)], wav, orig_sr, target_sr


def build_waveform_payload(wav_full, sample_rate):
    """Build the compact waveform representation stored with each task."""
    wav_mono = wav_full.mean(axis=1) if wav_full.ndim > 1 else wav_full
    total_dur = len(wav_mono) / sample_rate
    target_pts = max(2000, int(total_dur * 5))
    step = max(1, len(wav_mono) // target_pts)
    raw = [int(max(-32767, min(32767, wav_mono[i] * 32767)))
           for i in range(0, len(wav_mono), step)]
    import struct as _st
    return total_dur, _st.pack("<" + "h" * len(raw), *raw)


# ============================================================
# ASR 模块（多线程 + Qwen-Omni）
# ============================================================
_asr_init = False

def init_asr(api_key):
    global _asr_init
    if not _asr_init:
        import dashscope
        dashscope.api_key = api_key
        _asr_init = True

_quota_exhausted = False

def call_asr_one(seg_id, audio_chunk, sr, asr_cfg):
    """单个 segment 的 ASR 调用；限流会退避重试，失败返回 QUOTA。"""
    global _quota_exhausted
    if _quota_exhausted:
        return seg_id, "QUOTA"
    import random, tempfile, wave, struct
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_name = tmp.name
            with wave.open(tmp.name, "w") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
                frames = [int(max(-32767, min(32767, x*32767))) for x in audio_chunk]
                w.writeframes(struct.pack("<"+"h"*len(frames), *frames))

        from dashscope import MultiModalConversation
        max_retries = max(0, int(asr_cfg.get("max_retries", 5)))
        request_timeout = max(10, int(asr_cfg.get("request_timeout_s", 60)))
        for attempt in range(max_retries + 1):
            try:
                resp = MultiModalConversation.call(
                    model=asr_cfg.get("model", "qwen3.5-omni-plus"),
                    request_timeout=request_timeout,
                    messages=[{"role": "user", "content": [
                        {"audio": f"file://{tmp_name}"},
                        {"text": "请转写这段阿拉伯语音频，只输出阿拉伯语转写文本"}
                    ]}]
                )
            except Exception as exc:
                emsg = str(exc)
                if 'arrearage' in emsg.lower() or 'overdue payment' in emsg.lower():
                    _quota_exhausted = True
                    print("       ⏸️  DashScope账户欠费，暂停整批导入")
                    return seg_id, "ARREARAGE"
                is_rate_limit = '429' in emsg or 'rate' in emsg.lower()
                if is_rate_limit and attempt < max_retries:
                    wait = min(30, 2 ** attempt) + random.random()
                    time.sleep(wait)
                    continue
                if is_rate_limit:
                    print("       ⚠️  ASR限流重试耗尽，保存断点")
                else:
                    _quota_exhausted = True
                    print(f"       ⚠️  ASR异常，停止ASR: {type(exc).__name__}: {emsg[:300]}")
                return seg_id, "QUOTA"

            if resp.status_code == 200:
                choices = resp.output.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    if isinstance(content, list) and len(content) > 0:
                        return seg_id, content[0].get("text", "") or ""
                    return seg_id, str(content).strip()

            code = str(getattr(resp, 'code', '') or '')
            msg = str(getattr(resp, 'message', '') or '')
            is_rate_limit = (resp.status_code == 429 or 'rate' in msg.lower()
                             or 'rate' in code.lower())
            if is_rate_limit and attempt < max_retries:
                wait = min(30, 2 ** attempt) + random.random()
                time.sleep(wait)
                continue
            if is_rate_limit:
                print("       ⚠️  ASR限流重试耗尽，保存断点")
                return seg_id, "QUOTA"
            if 'arrearage' in code.lower() or 'overdue payment' in msg.lower():
                _quota_exhausted = True
                print("       ⏸️  DashScope账户欠费，暂停整批导入")
                return seg_id, "ARREARAGE"
            if 'DataInspection' in code or 'inappropriate' in msg.lower():
                print("       ⚠️  内容审核拦截，将保留源音频并跳过入库")
                return seg_id, "REJECT"

            _quota_exhausted = True
            print("       ⚠️  API调用失败，停止ASR")
            print(f"       状态码: {resp.status_code}")
            print(f"       错误码: {code}")
            print(f"       错误信息: {msg}")
            return seg_id, "QUOTA"
        return seg_id, "QUOTA"
    except Exception as e:
        _quota_exhausted = True
        print(f"       ⚠️  ASR准备失败: {type(e).__name__}: {str(e)[:300]}")
        return seg_id, "QUOTA"
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def process_audio_with_asr(audio_path, config, *, delete_rejected=False):
    """单个音频完整处理：VAD + 多线程ASR"""
    # 限流/异常状态只针对当前文件；自动降并发恢复时必须重新开启请求。
    global _quota_exhausted
    _quota_exhausted = False
    fname = Path(audio_path).name
    audio_dir = Path(config["audio_dir"])
    rel_path = Path(audio_path).relative_to(audio_dir).as_posix()
    folder = Path(rel_path).parent.as_posix()
    if folder == ".":
        folder = ""
    processing_token = None
    with db_conn() as conn, conn.cursor() as cur:
        existing = cur.execute(
            "SELECT id FROM annotation_tasks WHERE rel_path = %s", (rel_path,),
        ).fetchone()
        if existing:
            from annotation_metadata.processing import acquire_processing_lease, renew_processing_lease
            lease = acquire_processing_lease(cur, existing[0])
            processing_token = lease["token"]
            conn.commit()

    print(f"  🎙️  {fname}")
    t0 = time.time()

    # 1. VAD
    segs, wav_full, orig_sr, vad_sr = run_vad(audio_path, config.get("vad", {}))
    n_segs = len(segs)
    print(f"     VAD: {n_segs} 段 ({time.time()-t0:.1f}s)")

    if not segs:
        if delete_rejected:
            print("     ⚠️  未检测到语音，删除音频")
            audio_path.unlink(missing_ok=True)
        else:
            print("     ⚠️  未检测到语音，保留源音频并记录为不可领取")
            total_dur, waveform_payload = build_waveform_payload(wav_full, orig_sr)
            with db_conn() as conn:
                store_preprocessed_task(
                    conn, rel_path=rel_path, filename=fname, folder=folder,
                    duration=round(total_dur, 2), segments=[],
                    waveform_payload=waveform_payload,
                    preprocessed_at=datetime.now(timezone.utc), eligible=False,
                    task_extra={"preprocess_rejection": "no_speech",
                                "asr_checkpoint_incomplete": False},
                    processing_token=processing_token,
                )
        return {"status": "rejected_no_speech", "rel_path": rel_path}

    # 恢复未完成 ASR checkpoint（仅未分配、未人工修改的 draft）。
    with db_conn() as conn:
        checkpoint = load_draft_segments(conn, rel_path)
    checkpoint_by_id = {segment["id"]: segment for segment in checkpoint}
    for segment in segs:
        saved = checkpoint_by_id.get(segment["id"])
        if saved and abs(saved["start"] - segment["start"]) < 0.001 \
                and abs(saved["end"] - segment["end"]) < 0.001:
            segment["asr_text"] = saved["asr_text"]
            segment["text"] = saved["text"]

    # 2. 准备音频片段
    wav = wav_full.mean(axis=1) if wav_full.ndim > 1 else wav_full
    chunks = []
    for seg in segs:
        s_start = int(seg["start"] * orig_sr)
        s_end = int(seg["end"] * orig_sr)
        chunks.append(wav[s_start:s_end])

    # 3. 多线程ASR
    asr_cfg = config.get("asr", {})
    has_key = bool(asr_cfg.get("api_key", ""))
    workers = asr_cfg.get("workers", 10)
    _reject_file = False
    quota_stop = False
    arrearage_stop = False

    if processing_token:
        from annotation_metadata.processing import renew_processing_lease
        with db_conn() as conn, conn.cursor() as cur:
            row = cur.execute(
                "SELECT id FROM annotation_tasks WHERE rel_path = %s", (rel_path,),
            ).fetchone()
            if row:
                renew_processing_lease(cur, row[0], processing_token)
                conn.commit()

    if has_key:
        init_asr(asr_cfg["api_key"])
        # 只看没有asr_text的段
        todo = [(i, chunks[i]) for i in range(n_segs) if not segs[i].get("asr_text", "").strip()]
        print(f"     ASR: {len(todo)}/{n_segs} 段待转写 ({workers}线程)")

        done = 0; quota_stop = False; _reject_file = False
        # 只维持 workers 个在途请求。这样一旦内容审核拒绝或限流耗尽，
        # 可以立即停止派发余下片段，而不是让整文件的请求继续排队。
        todo_iter = iter(todo)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}

            def fill_slots():
                while len(futures) < workers and not quota_stop and not _reject_file:
                    try:
                        seg_i, _ = next(todo_iter)
                    except StopIteration:
                        break
                    future = pool.submit(call_asr_one, seg_i, chunks[seg_i], orig_sr, asr_cfg)
                    futures[future] = seg_i

            fill_slots()
            while futures:
                completed, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in completed:
                    futures.pop(future, None)
                    seg_i, text = future.result()
                    if text == "ARREARAGE":
                        arrearage_stop = True
                        quota_stop = True
                    elif text == "QUOTA":
                        quota_stop = True
                    elif text == "REJECT":
                        _reject_file = True
                    elif text and not text.startswith("ERR:"):
                        segs[seg_i]["asr_text"] = text
                        segs[seg_i]["text"] = text
                    done += 1
                    if done % 20 == 0:
                        print(f"       ASR进度: {done}/{len(todo)}")
                fill_slots()
        if quota_stop and not _reject_file:
            print(f"     ⚠️  ASR 中断，已处理 {done}/{len(todo)} 段 ({time.time()-t0:.1f}s)")
        elif not _reject_file:
            print(f"     ASR完成 ({time.time()-t0:.1f}s)")
    else:
        print(f"     ⚠️  未配置API Key，跳过ASR")
        quota_stop = True

    # 内容审核拦截默认保留源文件；只有显式参数才允许删除。
    if _reject_file:
        if delete_rejected:
            print(f"     🗑️  删除被拒绝的音频: {fname}")
            audio_path.unlink(missing_ok=True)
        else:
            print(f"     ⏭️  保留被拒绝的源音频并记录为不可领取: {fname}")
            total_dur, waveform_payload = build_waveform_payload(wav_full, orig_sr)
            with db_conn() as conn:
                store_preprocessed_task(
                    conn, rel_path=rel_path, filename=fname, folder=folder,
                    duration=round(total_dur, 2), segments=segs,
                    waveform_payload=waveform_payload,
                    preprocessed_at=datetime.now(timezone.utc), eligible=False,
                    task_extra={"preprocess_rejection": "content_inspection",
                                "asr_checkpoint_incomplete": True},
                    processing_token=processing_token,
                )
        return {"status": "rejected_content", "rel_path": rel_path}

    # 任何空转写都作为可续跑 checkpoint，不能进入标注员领取池。
    if any(not (segment.get("asr_text") or "").strip() for segment in segs):
        quota_stop = True

    # 4. 生成波形缓存（base64 int16）
    total_dur, waveform_payload = build_waveform_payload(wav_full, orig_sr)

    # 5. 事务写入 PostgreSQL；连接只在写入期间持有，不跨 VAD/ASR。
    with db_conn() as conn:
        result = store_preprocessed_task(
            conn,
            rel_path=rel_path,
            filename=fname,
            folder=folder,
            duration=round(total_dur, 2),
            segments=segs,
            waveform_payload=waveform_payload,
            preprocessed_at=datetime.now(timezone.utc),
            category=None,
            skip_if_human_modified=True,
            processing_token=processing_token,
            eligible=not quota_stop,
            task_extra={"asr_checkpoint_incomplete": bool(quota_stop),
                        "preprocess_rejection": None},
        )
    if result["action"] == "skipped_human":
        print(f"     ⏭️  已有人工标注，未覆盖: {fname}")
        return {"status": "skipped_human", "rel_path": rel_path}
    elif quota_stop:
        print(f"     ✅ checkpoint 已保存且保持不可领取: {fname}")
        if arrearage_stop:
            raise AsrArrearage("DashScope账户欠费，已保存断点并暂停整批导入")
        raise AsrInterrupted("ASR interrupted after checkpoint was saved")
    else:
        print(f"     ✅ 已写入 PostgreSQL: {fname} ({result['action']})")
        return {"status": result["action"], "rel_path": rel_path,
                "segments": len(segs)}


# ============================================================
# 主流程
# ============================================================
def main():
    config = load_config()
    parser = argparse.ArgumentParser(description="音频批量预处理 VAD+ASR (500h优化版)")
    parser.add_argument("--audio-dir", "-d", default=config.get("audio_dir", "./audio"))
    parser.add_argument("--workers", "-w", type=int, default=config.get("asr", {}).get("workers", 10),
                        help="ASR并发线程数(默认10)")
    parser.add_argument("--force", "-f", action="store_true", help="强制重新处理所有文件")
    parser.add_argument("--batch-subdir", help="只扫描 audio_dir 下的指定相对目录，入库路径仍保留该目录前缀")
    parser.add_argument("--limit", type=int, help="最多处理 N 个待处理音频（用于小样本验证）")
    parser.add_argument("--dry-run", action="store_true", help="只盘点待处理文件，不运行VAD/ASR或写数据库")
    parser.add_argument("--delete-rejected", action="store_true",
                        help="删除无语音或内容审核拒绝的源音频（默认始终保留）")
    parser.add_argument("--retry-rejected", action="store_true",
                        help="重新处理此前被VAD或内容审核拒绝的不可领取任务")
    parser.add_argument("--manifest", help="crawler JSONL/JSON manifest for source metadata")
    parser.add_argument("--batch-code", help="globally unique source batch code")
    parser.add_argument("--source-audio-root", help="crawler audio root used to map relative_path")
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.manifest and not args.batch_code:
        parser.error("--manifest 需要同时提供 --batch-code")

    if args.manifest:
        from annotation_metadata.ingestion import import_source_metadata
        from db import db_conn as _db_conn
        manifest_path = Path(args.manifest).resolve()
        source_root = Path(args.source_audio_root).resolve() if args.source_audio_root else None
        audio_root = Path(args.audio_dir).resolve()
        with _db_conn() as conn:
            imported = import_source_metadata(
                conn, manifest_path=manifest_path, batch_code=args.batch_code,
                source_root=source_root, audio_root=audio_root,
                dry_run=args.dry_run,
            )
        print(json.dumps({k: imported[k] for k in imported if k != "results"},
                         ensure_ascii=False, indent=2, default=str))
        if args.dry_run:
            return
        # Continue into the existing directory scan so ASR can fill placeholders.
        # Metadata-only protected tasks are skipped by the existing human-work guard.

    config["audio_dir"] = str(Path(args.audio_dir).resolve())
    config["asr"]["workers"] = args.workers

    audio_dir = Path(config["audio_dir"])
    scan_dir = audio_dir
    if args.batch_subdir:
        batch_subdir = Path(args.batch_subdir)
        if batch_subdir.is_absolute():
            parser.error("--batch-subdir 必须是 audio_dir 下的相对路径")
        scan_dir = (audio_dir / batch_subdir).resolve()
        try:
            scan_dir.relative_to(audio_dir)
        except ValueError:
            parser.error("--batch-subdir 不能超出 audio_dir")
        if not scan_dir.is_dir():
            parser.error(f"批次目录不存在: {scan_dir}")

    # 收集音频文件
    audio_files = []
    for root, dirs, files in os.walk(scan_dir):
        dirs[:] = [d for d in dirs if d != "annotations" and not d.startswith(".")]
        for f in sorted(files):
            if Path(f).suffix.lower() in AUDIO_EXTENSIONS and not f.startswith("."):
                audio_files.append(Path(root) / f)
    audio_files.sort(key=lambda path: path.relative_to(audio_dir).as_posix())

    if not audio_files:
        print("❌ 未找到音频文件")
        return

    # 一次查询全部任务状态，避免 10 万音频逐条访问数据库。
    with db_conn() as conn:
        known = ingestion_states(conn)
    pending = []
    for af in audio_files:
        rel = af.relative_to(audio_dir).as_posix()
        if rel in known:
            state = known[rel]
            if state["protected"]:
                reasons = []
                if state.get("assigned"):
                    reasons.append("已分配")
                if state.get("human_modified"):
                    reasons.append("人工修改")
                if state.get("published"):
                    reasons.append("已发布")
                print(f"⏭️  {'/'.join(reasons)}，跳过: {rel}")
                continue
            if state.get("rejection") and not args.retry_rejected:
                print(f"⏭️  已记录为不可领取 ({state['rejection']})，跳过: {rel}")
                continue
            if state["eligible"] and not args.force:
                print(f"⏭️  已预处理，跳过: {rel}")
                continue
            if not state["eligible"]:
                print(f"↻ 恢复未完成 ASR checkpoint: {rel}")
        pending.append(af)

    print(f"\n{'='*56}\n  🎙️  音频预处理 (VAD+ASR)  500h优化版\n{'='*56}")
    matched_pending = len(pending)
    if args.limit is not None:
        pending = pending[:args.limit]

    print(f"  音频根目录: {config['audio_dir']}")
    print(f"  扫描目录: {scan_dir}")
    print(f"  总文件: {len(audio_files)} | 待处理: {matched_pending} | 本次处理: {len(pending)}")
    print(f"  ASR: {'已配置' if config['asr']['api_key'] else '❌ 未配置'} | 线程: {args.workers}")
    print(f"  VAD: min_speech={config['vad']['min_speech_duration_ms']}ms\n")

    if args.dry_run:
        for af in pending[:20]:
            print(f"  DRY-RUN: {af.relative_to(audio_dir).as_posix()}")
        if len(pending) > 20:
            print(f"  ... 其余 {len(pending) - 20} 个")
        print("✅ dry-run 完成；未运行VAD/ASR，未写数据库")
        return

    if not pending:
        print("✅ 全部处理完毕")
        return

    # 预热VAD
    print("⏳ 加载VAD模型...")
    get_vad()
    print("✅ 就绪\n")

    t_total = time.time()
    original_workers = args.workers
    recovery_workers = max(1, min(original_workers, 3))
    deferred = []

    def run_one(af, *, workers):
        config["asr"]["workers"] = workers
        try:
            process_audio_with_asr(af, config, delete_rejected=args.delete_rejected)
            return True
        except AsrArrearage:
            raise
        except AsrInterrupted as e:
            print(f"  ⛔ {e}")
            return False
        except Exception as e:
            print(f"  ❌ 失败: {e}")
            return False

    try:
        for i, af in enumerate(pending, 1):
            print(f"[{i}/{len(pending)}]", end=" ")
            if not run_one(af, workers=original_workers):
                print(f"  ↻ 自动降至 {recovery_workers} 路恢复: {af.name}")
                if not run_one(af, workers=recovery_workers):
                    deferred.append(af)

        # 把仍受限的文件放到本轮末尾再试一次，避免单个账号限流阻塞整批。
        for retry_round in range(1, 3):
            if not deferred:
                break
            retrying = deferred
            deferred = []
            print(f"\n↻ 第 {retry_round} 轮重试断点文件: {len(retrying)} 个 ({recovery_workers}路)")
            for af in retrying:
                print(f"  ↻ 重试: {af.name}")
                if not run_one(af, workers=recovery_workers):
                    deferred.append(af)
    except AsrArrearage as e:
        config["asr"]["workers"] = original_workers
        print(f"\n⏸️  {e}")
        raise SystemExit(2)

    config["asr"]["workers"] = original_workers
    if deferred:
        print(f"\n⚠️ 仍有 {len(deferred)} 个文件保留断点；可再次运行命令继续")
        raise SystemExit(1)
    print(f"\n✅ 全部完成 (总耗时 {time.time()-t_total:.0f}s)")


if __name__ == "__main__":
    main()
