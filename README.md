# 在线课程录播器（AI LessonRecorder）

![版本](https://img.shields.io/badge/version-1.0.0--Beta-blue) ![平台](https://img.shields.io/badge/platform-Windows-informational) ![Python](https://img.shields.io/badge/Python-3.11%2B-green)

一款 Windows 桌面端软件，用于录制在线视频课程，自动将课程音频实时转成带时间戳的逐字稿（Markdown），同时智能检测 PPT 页面翻页并截取截图、按时间戳插入到逐字稿中，最终借助 AI 将逐字稿梳理归纳为结构化的课程教程文档，并可导出为图文并茂的 Word 文档。

## 核心功能

| 功能 | 说明 |
|------|------|
| 🎙️ 系统声音录制 | 捕获电脑播放的在线课程声音（WASAPI 环回录音，无需麦克风） |
| 📝 实时语音转文本 | 边录边转，生成带时间戳的逐字稿，实时显示 |
| 🖼️ PPT 翻页检测与截图 | 定时抓取屏幕/窗口画面，通过图像相似度算法判断 PPT 是否翻页，翻页即截图保存 |
| 🕐 时间轴同步 | 截图按时间戳自动插入到逐字稿中对应位置，生成图文混排的 MD 文件 |
| 📄 MD → Word 转换 | 将逐字稿 MD 转换为 Word 文档，截图插入到对应段落 |
| 🤖 AI 课程文档生成 | 调用大语言模型对逐字稿进行梳理、归纳、层级编排，生成详细的结构化教程文档，并配上 PPT 截图 |

## 技术栈（初定）

- **桌面框架**：Python 3.11+ / PySide6（Qt6）
- **音频采集**：PyAudioWPatch（WASAPI Loopback 系统声卡环回录音）
- **语音识别**：本地 faster-whisper（离线）/ 云端 ASR API（可选，可配置）
- **截图与图像识别**：mss（截图）+ OpenCV / 感知哈希（pHash，翻页检测）
- **AI 梳理总结**：大语言模型 API（如 DeepSeek / 通义千问 / OpenAI 兼容接口，可配置）
- **文档生成**：Markdown 渲染 + python-docx（Word 导出）
- **打包发布**：PyInstaller（生成单个 EXE）

## 目录结构

```
4_AI-LessonRecorder/
├── README.md                 # 项目说明（本文件）
├── main.py                   # 程序入口
├── requirements.txt          # Python 依赖清单
├── Docs/
│   ├── 软件需求说明.md        # 功能/非功能需求规格
│   └── 软件方案设计.md        # 架构与技术方案设计
├── config/                   # 配置文件（ASR/LLM/检测阈值等）
├── assets/                   # 图标、样式等静态资源
├── src/
│   ├── app/                  # 应用启动、全局状态管理
│   ├── core/                 # 核心引擎：录音、转写调度、翻页检测、时间轴同步
│   ├── ai/                   # AI 能力层：ASR、图像变化识别、LLM 梳理总结
│   ├── export/               # 文档导出：MD 整合、Word 生成
│   ├── ui/                   # 界面层：主窗口、控制面板、实时字幕区
│   └── utils/                # 通用工具：日志、时间处理、文件管理
├── output/                   # 录制输出目录（逐字稿/截图/成品文档）
└── tests/                    # 单元测试
```

## 快速开始（开发环境）

```bash
# 1. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动应用
python main.py
```

## 使用流程

```
开始录制 ──> 捕获系统声音 ──> 实时转文字（带时间戳）
   │
   └──────> 定时抓屏 ──> 检测 PPT 翻页 ──> 截图保存（带时间戳）
                              │
停止录制 ──> 合成逐字稿.md（文本 + 截图混排）
                              │
   ┌──────────────────────────┴──────────────────────────┐
   │                                                      │
导出 Word 逐字稿文档                        AI 梳理总结 ──> 生成结构化教程文档.md/Word
```

## 输出示例

生成的逐字稿 MD 结构示意：

```markdown
# 课程逐字稿 - 2026-09-27 14-30-00

## [00:00:12] 第 1 页
![PPT 00-00-12](screenshots/000012_slide_001.png)

各位同学大家好，今天我们来讲一下……

## [00:03:45] 第 2 页
![PPT 00-03-45](screenshots/000225_slide_002.png)

接下来我们看第二个知识点……
```

AI 梳理后的教程文档结构示意：

```markdown
# 课程教程：XXX 专题

## 一、本节课概览
## 二、知识点详解
### 2.1 XXX（配 PPT 截图）
### 2.2 XXX（配 PPT 截图）
## 三、要点总结与复习提纲
```

## 开发路线图

- [x] 阶段 0：需求分析与方案设计（本文档系列）
- [x] 阶段 1：音频录制 + 实时语音转文本（WASAPI 环回 + faster-whisper VAD 分段）
- [x] 阶段 2：PPT 翻页检测 + 截图 + 时间轴同步 MD 生成（pHash+SSIM 双校验、冷却防抖、翻回复用）
- [x] 阶段 3：Word 导出（逐字稿版，python-docx）
- [x] 阶段 4：AI 梳理总结，生成结构化教程文档（Map-Reduce 分治 + `{{slide:N}}` 占位插图）
- [x] 阶段 5：PySide6 桌面应用集成（录制控制/实时字幕/截图缩略图/设置/历史会话/崩溃恢复）
- [ ] 阶段 6：真机实测调优（不同课程平台、翻页灵敏度、ASR 模型档位）
- [ ] 阶段 7：打包发布（PyInstaller EXE）

## 许可证

私有项目，仅供个人学习使用。
