# -*- coding: utf-8 -*-
"""在线课程录播器（AI LessonRecorder）程序入口

支持两种模式：
- 正常模式：双击运行 GUI
- 自检模式：`--selftest [输出json]` / `--selftest-to <输出json>`
  不弹窗、不录音、不截图，纯离线跑通「配置 → 会话 → 事件落盘 → MD/Word 导出
  → 密钥加解密 → 视觉判据 → UI 构建」全链路，写出JSON 报告供打包自检断言。

**安全约定**：进入自检模式时会置`LESSONRECORDER_SELFTEST=1` 环境变量，
`SessionManager.start()` 见到该变量直接拒绝启动——
即使参数解析出意外，也绝不会在自检/自动化探测时真的去录音和截屏。
"""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

# 保证以任意工作目录启动时都能找到 src 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _selftest_arg(argv: list[str]) -> str | None:
    """解析自检参数，返回输出文件路径（None 表示只打印）

    宽容解析：接受 `--selftest`、`--selftest-to <path>`、`--selftest=<path>`，
    避免因为写法差异（如 `=` 与空格）落回 GUI 分支——那会导致自动化探测时
    真的启动 GUI 与录音。
    """
    for i, a in enumerate(argv):
        if a == "--selftest":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            return nxt if nxt and not nxt.startswith("--") else None
        if a == "--selftest-to" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--selftest="):
            return a.split("=", 1)[1] or None
    return None


def _is_selftest_requested(argv: list[str]) -> bool:
    return any(a == "--selftest" or a.startswith("--selftest")
               for a in argv)


def _selftest(out_path: str | None) -> int:
    """离线自检：不依赖音频设备与网络"""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["LESSONRECORDER_SELFTEST"] = "1"   # 硬保险：禁止真实录制
    report: dict = {"ok": False, "checks": [], "root": str(Path(__file__).parent)}

    def ck(name: str, cond: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "ok": bool(cond), "detail": detail})
        print(f"  [{'OK  ' if cond else 'FAIL'}] {name}"
              + (f"  — {detail}" if detail else ""), flush=True)

    try:
        from PySide6.QtWidgets import QApplication
        from src.app.application import AppContext
        from src.core.session_store import SessionStore, create_session
        from src.core.timeline import SlideEvent, TranscriptEvent
        from src.core.vision.change_algo import dhash, diff_ratio, hamming, phash, ssim
        from src.app.session_manager import SessionManager
        from src.utils.secrets_store import encrypt_secret, decrypt_secret
        import numpy as np

        app = QApplication.instance() or QApplication([])

        # 1) 应用上下文（配置 / 日志 / 密钥体系）
        ctx = AppContext()
        ck("AppContext 初始化", True, str(ctx.root))

        # 2) 密钥加解密往返
        secret = "sk-selftest-ABC123"
        enc = encrypt_secret(secret)
        dec = decrypt_secret(enc)
        ck("DPAPI 加密往返", dec == secret, f"密文前缀 {enc[:12]}")
        ck("磁盘无明文", secret not in enc)

        # 3) 会话创建 + 事件落盘 + MD/Word 导出
        with tempfile.TemporaryDirectory(prefix="lr_selftest_") as tmp:
            paths = create_session(Path(tmp), "自检课程")
            store = SessionStore(paths)
            for i in range(5):
                store.append_event(TranscriptEvent(ts=i * 2.0,
                                                   text=f"第{i}句测试内容", conf=0.9))
            store.append_event(SlideEvent(ts=1.0, image="slide_001.png", index=1))
            store.close()
            ck("会话目录创建", paths.root.exists(), str(paths.root))

            md = SessionManager.rebuild_transcript(str(paths.root))
            ck("逐字稿 MD 生成", md.exists() and md.stat().st_size > 0, str(md))

            from src.core.session_store import SessionStore as _SS
            evs = _SS(paths).load_events()
            ck("事件流恢复", len(evs) == 6, f"读到 {len(evs)} 条事件")

            docx = SessionManager.export_word(str(paths.root), include_images=False)
            ck("Word 导出", docx.exists() and docx.stat().st_size > 0, str(docx))

            paths.write_meta(finished=True, duration=10.0,
                             transcript_count=5, slide_count=1,
                             config=ctx.config.as_dict(),
                             stats={"asr": {"segments_out": 5}})
            meta = paths.read_meta()
            ck("元数据含运行时指标", "stats" in meta,
               f"transcript_count={meta.get('transcript_count')}")

        # 4) 视觉判据可运行（pHash/dHash 返回 64bit bool 数组，用 hamming 比较）
        from src.core.vision.change_algo import hamming
        a = (np.random.RandomState(0).rand(64, 64) * 255).astype(np.uint8)
        b = a.copy()
        b[:32, :] = 255 - b[:32, :]          # 制造"换页"级差异
        ck("pHash 同图距离为 0", hamming(phash(a), phash(a)) == 0.0)
        ck("pHash 换页可辨", hamming(phash(a), phash(b)) > 0.1,
           f"pHash 距离={hamming(phash(a), phash(b)):.3f}")
        ck("dHash 同图距离为 0", hamming(dhash(a), dhash(a)) == 0.0)
        ck("dHash 换页可辨", hamming(dhash(a), dhash(b)) > 0.1,
           f"dHash 距离={hamming(dhash(a), dhash(b)):.3f}")
        ck("SSIM 同图≈1", abs(ssim(a, a) - 1.0) < 1e-6, f"{ssim(a, a):.4f}")
        ck("SSIM 换页可辨", ssim(a, b) < 0.95, f"{ssim(a, b):.4f}")
        ck("diff_ratio 换页可辨", diff_ratio(a, b) > 0.02,
           f"{diff_ratio(a, b):.4f}")

        # 5) 配置读写往返
        cfg_file = Path(tempfile.mkdtemp(prefix="lr_cfg_")) / "settings.json"
        from src.utils.config import AppConfig
        c = AppConfig(cfg_file)
        c.set("llm.api_key", secret)
        c.save()
        raw = cfg_file.read_text(encoding="utf-8")
        ck("配置落盘无明文密钥", secret not in raw)
        c2 = AppConfig(cfg_file)
        ck("配置重载还原明文", c2.get("llm.api_key") == secret)

        # 6) UI 可构建（自检模式下 MainWindow 不得启动任何录制/截图线程）
        from src.ui.main_window import MainWindow
        win = MainWindow(ctx)
        ck("主窗口构建", win is not None)
        ck("自检模式未启动录制", win.manager.state == "idle"
           and not win.manager.is_busy, f"state={win.manager.state}")
        win.hotkeys.unregister_all()
        win.deleteLater()
        app.processEvents()

        report["ok"] = all(c["ok"] for c in report["checks"])
    except Exception as e:  # noqa: BLE001
        report["error"] = f"{type(e).__name__}: {e}"
        report["traceback"] = traceback.format_exc()[-2000:]

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if out_path:
        try:
            Path(out_path).write_text(text, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            report["error"] = f"写入自检报告失败: {e}"
            text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    return 0 if report.get("ok") else 1


def main() -> int:
    argv = sys.argv[1:]
    if _is_selftest_requested(argv):
        return _selftest(_selftest_arg(argv))

    from PySide6.QtWidgets import QApplication
    from src.app.application import AppContext
    from src.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("在线课程录播器")
    app.setOrganizationName("HIDOTEK")

    ctx = AppContext.instance()  # 加载配置、初始化日志
    win = MainWindow(ctx)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())