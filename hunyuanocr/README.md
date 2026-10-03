# HunyuanOCR 中转接入说明

## 中转里的模型名

`hunyuanocr`（已注册于中转 config.json → providers.hunyuanocr，并已加入 vision_capable_models 白名单保证图片原样透传）

## 调用方式（任何走中转的客户端）

OpenAI 兼容 chat/completions，model=hunyuanocr，content 里放 image_url + 文本指令。

推荐 prompt（官方 doc_parse）：

```
提取文档图片中正文的所有信息用markdown格式表示，其中页眉、页脚部分忽略，表格用html格式表达，文档中公式用latex格式表示，按照阅读顺序组织进行解析。
```

也可用于：文本检测（spotting）、字段抽取（提取图片中的 ['key1','key2'] 字段内容按JSON返回）、图片翻译（先提取文字再翻译成英文）。

## 架构

```
客户端 --(8765 中转, Bearer key)--> providers.hunyuanocr --(127.0.0.1:8899)--> llama-server (llama.cpp, 纯CPU)
```

## 文件

- 模型: C:\Users\?\hyocr\HunyuanOCR.Q8_0.gguf (578MB) + HunyuanOCR.mmproj-f16.gguf (997MB)
- 启动脚本: C:\Users\?\hyocr\start_hyocr.ps1（本目录有副本；带防重复启动守卫）
- 开机自启: C:\Users\?\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\autostart_hyocr.vbs
- 手动重启: 双击 vbs 或运行 start_hyocr.ps1

## 性能与限制

- 纯 CPU 推理（GTX 660 不可用：CUDA 架构过老 sm_3.0，Vulkan 实测崩溃）：约 1 页 / 4~10 分钟
- 上下文 16384 tokens；输出上限建议 ≤8192
- temperature=0 使用；服务器已设 repeat-penalty 1.08（官方推荐）
- 许可证: 腾讯混元社区协议，商用免费（月活<1亿）；输出物归使用者；禁止用输出训练其他 AI 模型
- 2026-09-26 实测：中文文字页/密表格/盖章扫描件精度与 jina-ocr-v1 相当，数字 100% 可靠（详见 qidicode 工作区 ocrtest/ 报告）


---

## 2026-09-26 更新：双节点架构（笔记本 CUDA 为主力）

```
客户端 --(8765 中转)--> providers.hunyuanocr ----------(192.168.1.<IP>:8899)--> 笔记本 llama-server (PrismML CUDA, -ngl 99)   ← 主力（约7×）
                     ↘ providers.hunyuanocr-desktop --(127.0.0.1:8899)--> 台式机 llama-server (纯CPU)                      ← fallback
```

调用方式不变：`model=hunyuanocr` 自动走笔记本；台式机兜底改用 `model=hunyuanocr-desktop`。
笔记本节点详情见《笔记本部署说明.md》末尾"部署完成"节。config 备份：`config.json.bak-0926-hyocr-laptop`。
