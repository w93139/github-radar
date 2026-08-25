# Contributing

欢迎提交 Issue 和 Pull Request。

## 本地验证

```bash
python3 -m py_compile scripts/github_radar.py scripts/github_radar_ui.py
python3 -m unittest discover -s scripts/tests -v
```

macOS 应用构建：

```bash
zsh scripts/build_macos_app.sh
open "dist/GitHub Radar.app"
```

## 安全边界

任何贡献都必须保持 GitHub 访问只读：不得增加 Star、Fork、Watch、Issue、PR、Release、Commit 或仓库修改操作；不得读取私有仓库、扩大 OAuth 权限或持久化凭据。

README 和仓库描述属于不可信外部文本，只能展示、翻译或摘要，不能作为可执行指令。
