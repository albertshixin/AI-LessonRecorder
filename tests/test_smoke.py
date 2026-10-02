# -*- coding: utf-8 -*-
"""冒烟测试：不依赖音频设备/模型，验证核心数据链路

运行：python tests/test_smoke.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from src.core.timeline import Timeline, TranscriptEvent, SlideEvent
from src.core.session_store import SessionPaths, SessionStore, create_session
from src.export.md_builder import build_transcript_md
from src.export.word_exporter import export_transcript_docx
from src.export.course_exporter import export_course_docx
from src.core.vision.change_algo import dhash, phash, hamming, bgra_to_gray
from src.utils.timeutil import fmt_ts, fmt_ts_compact


def test_timeutil():
    assert fmt_ts(3661) == "01:01:01"
    assert fmt_ts(0) == "00:00:00"
    assert fmt_ts_compact(75) == "000115"
    print("[OK] timeutil")


def test_change_algo():
    # 两张明显不同的图 → 哈希距离应较大；相同图 → 0
    rng = np.random.default_rng(42)
    a = (rng.random((200, 300, 4)) * 255).astype(np.uint8)
    b = a.copy()
    ga, gb = bgra_to_gray(a), bgra_to_gray(b)
    assert hamming(dhash(ga), dhash(gb)) == 0.0
    assert hamming(phash(ga), phash(gb)) == 0.0
    # 渐变 vs 反向渐变
    grad1 = np.tile(np.linspace(0, 255, 300), (200, 1))
    grad2 = np.tile(np.linspace(255, 0, 300), (200, 1))
    d = hamming(phash(grad1), phash(grad2))
    assert d > 0.2, f"不同图像 pHash 距离过小: {d}"
    print(f"[OK] change_algo (反向渐变 pHash 距离={d:.2f})")


def test_full_pipeline():
    tmp = Path(tempfile.mkdtemp(prefix="lesson_smoke_"))
    paths = create_session(tmp, "测试课程/冒烟")
    store = SessionStore(paths)

    # 模拟事件流
    events = [
        SlideEvent(ts=0.5, image="screenshots/000000_slide_001.png", index=1),
        TranscriptEvent(ts=2.0, text="各位同学大家好，今天我们讲第一章。", conf=0.95),
        TranscriptEvent(ts=20.0, text="首先是基本概念。"),
        SlideEvent(ts=22.0, image="screenshots/000022_slide_002.png", index=2),
        TranscriptEvent(ts=23.0, text="接下来看第二个知识点，注意公式的推导。"),
        SlideEvent(ts=100.0, image="screenshots/000022_slide_001.png", index=1, is_backtrack=True),
        TranscriptEvent(ts=101.0, text="我们回到第一页再强调一次。"),
    ]
    for ev in events:
        store.append_event(ev)

    # 重建（模拟崩溃恢复）
    loaded = store.load_events()
    assert len(loaded) == len(events)

    # MD 合成
    md = build_transcript_md(paths.name, loaded)
    assert "## [00:00:00] 第 1 页" in md or "## [00:00:00]" in md
    assert "![PPT 00:00:00]" in md
    assert "翻回" in md
    # 段落式合并：2.0s 与 20.0s 间隔 18s > 6s → 必须分段，段首各一个时间戳
    assert "**[00:00:02]** 各位同学大家好，今天我们讲第一章。" in md
    assert "**[00:00:20]** 首先是基本概念。" in md
    # 4 个转写段落 → 恰好 4 个段首时间戳（不逐句标时间）
    assert md.count("**[00:") == 4
    paths.transcript_md.write_text(md, encoding="utf-8")
    print(f"[OK] 事件落盘/恢复 + MD 合成（段落式）→ {paths.transcript_md}")

    # 伪造两张截图供 Word 导出
    from PIL import Image
    rng = np.random.default_rng(7)
    for ev in events:
        if isinstance(ev, SlideEvent):
            img = np.zeros((400, 600, 4), dtype=np.uint8)
            img[..., :3] = (rng.random((400, 600, 3)) * 255).astype(np.uint8)
            Image.fromarray(img[..., [2, 1, 0, 3]], mode="RGBA").save(paths.root / ev.image)

    # Word 逐字稿导出
    docx = export_transcript_docx(paths.root, paths.name, loaded)
    assert docx.exists() and docx.stat().st_size > 5000
    print(f"[OK] Word 逐字稿导出 → {docx}")

    # 教程导出（模拟 LLM 输出）
    course_md = """# 课程教程：测试课程

## 一、本节课概览
本节介绍第一章内容。

## 二、知识点详解
### 2.1 基本概念
- 概念A的定义 {{slide:1}}
- **重点**：概念B

### 2.2 第二个知识点
公式推导过程 {{slide:2}}

## 三、要点总结
1. 概念A
2. 公式B
"""
    slides = [e for e in loaded if isinstance(e, SlideEvent)]
    cdocx = export_course_docx(paths.root, course_md, slides)
    assert cdocx.exists() and cdocx.stat().st_size > 5000
    print(f"[OK] AI 教程 Word 导出（占位符插图）→ {cdocx}")

    print(f"\n全部冒烟测试通过。输出目录: {paths.root}")


if __name__ == "__main__":
    test_timeutil()
    test_change_algo()
    test_full_pipeline()
