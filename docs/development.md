# 开发与验证

[返回 README](../README.md)

先按[安装指南](getting-started.md#不使用-docker)在本机运行后端。生产依赖由 `pyproject.toml` 声明，镜像使用带完整哈希的 `requirements.lock`（Linux amd64 / Python 3.12），前端使用 `frontend/package-lock.json`。

## 前端开发

后端保持运行，另开终端：

```bash
npm --prefix frontend run dev
```

访问 <http://127.0.0.1:5173>，Vite 把 `/api` 代理到本机 `8766`。生产环境由后端直接提供 `frontend/dist/`，构建产物不加入版本控制。

## 代码组织

后端按领域分包，各包职责见[架构](architecture.md#组件)。新增 HTTP 接口时，在 `jav_pilot/api/routing.py` 登记路径，在对应的 `api/routes/` 路由组中实现 `_handle_*` 方法；后台管理器的创建与关闭放在 `api/services/`，进程级单例、锁和缓存放在 `api/state.py` 并通过 `state.X` 读写。新增前端路由还需加入 `jav_pilot/api/static_files.py` 的 `APP_ROUTES`，否则刷新或直接打开链接会返回 JSON 404。

包内模块之间共用的函数不加下划线前缀，只在本模块使用的保留下划线。以 `python -m` 启动的 worker 使用完整模块路径（例如 `jav_pilot.web_download.worker`），移动模块时需同步更新调用方。顶层的 `auth.py`、`backup_restore.py`、`schema_contract.py`、`browser_acceptance.py` 是发布工具跨版本使用的导入路径，不要移除。

## 站点适配

站点改版时：

1. 保存最小、脱敏且有使用权限的 HTML 或响应样本，放在被 Git 忽略的 `test/` 中。
2. 区分域名变化、模板变化、DOM 变化、人机验证、地区限制和媒体主机变化。
3. 域名和模板问题优先修改站点设置；DOM 问题修改对应的解析阶段。
4. 保留空结果与失败的区别，确认一个来源失败时其他来源的结果仍可展示。
5. 修改浏览器或媒体传输时，验证重定向、主机白名单、临时状态清理和断点身份。
6. 运行相关测试后，显式执行在线检查 `python -m jav_pilot.cli smoke-sites --code '<番号>'`。

适配器必须遵守的约定见[架构](architecture.md#外部来源)。

## 检查

```bash
python -m pip install -e '.[dev]'
python -m ruff check jav_pilot ops tools
python -m compileall -q jav_pilot ops tools
npm --prefix frontend run build
python tools/frontend_budget.py frontend/dist
```

测试和测试资产放在仓库根目录的 `test/`，该目录被 Git 忽略，不随源码发布。已有测试集时：

```bash
python -m pytest -q test/python/tests/<相关测试文件>
npm --prefix frontend test -- ../test/frontend/<相关测试文件>
```

pytest 默认排除 `e2e`、`load`、`online` 标记。不要把真实 Cookie、影片、个人数据库或生产配置当作测试样本。

浏览器端到端测试和 Lighthouse 需要本地测试资产 `test/support/e2e_fixture_server.py`：

```bash
python tools/e2e_run.py --browsers chromium,firefox,webkit
python tools/lighthouse_gate.py
```

`tools/web_download_fault_matrix.py` 在隔离的临时目录中对 Web 下载做故障注入（进程被杀、磁盘不足、存储断连、服务重启等），需要 Linux；只能使用临时目录和测试媒体。

## 依赖与镜像

修改 Python 生产依赖的版本后，在 Python 3.12 / Linux amd64 环境运行 `python tools/refresh_python_lock.py` 刷新锁文件。它检查完整依赖闭包、哈希和已公开的漏洞，失败时不替换现有锁。

构建并发布镜像：

```bash
docker build --platform linux/amd64 --build-arg VCS_REF="$(git rev-parse HEAD)" \
  -t drdon1234/jav-pilot:<版本> -t drdon1234/jav-pilot:latest .
docker push drdon1234/jav-pilot:<版本>
docker push drdon1234/jav-pilot:latest
```

镜像只包含运行所需的文件；需要代理时通过构建参数传入，不要写进 Dockerfile 或镜像历史。

## 修改原则

修复先确认根因；新增行为要覆盖失败和恢复路径。涉及配置、任务库、迁移、文件替换或归档时，保持原数据可追溯，并提供冲突检测与恢复手段。

## 公开源码

`python tools/public_release.py` 检查公开文件的命名和常见凭据特征；加上 `--output '<目录>/jav-pilot-source.zip'` 导出经过白名单筛选的源码，其中不含 `.git`、`test/`、`.private/` 和运行数据。曾经提交过私人信息的 Git 历史无法靠删除文件清理，应以核查后的源码包创建新的公开仓库。安全要求见[安全说明](../SECURITY.md)。
