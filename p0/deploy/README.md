# 部署说明（第二增量）

当前服务器（123.57.11.86）使用 **venv + systemd** 运行：
`/opt/cq-ai-p0`，服务名 `cq-p0-api`，监听 `127.0.0.1:8090`。

标准解析依赖 `pypdf==6.21.4`（已装入服务器 venv）。服务器 pip 镜像当前不可用，
更新代码时不要再执行 `pip install -r requirements.txt`；重建环境时用
`pip install --index-url https://pypi.org/simple pypdf==6.21.4`，或把 wheel 解压到
`/opt/cq-ai-p0/vendor/`。

## 更新代码

```bash
# 本地
tar czf - -C p0 app static tests requirements.txt Dockerfile docker-compose.yml deploy README.md \
  | ssh -i .deploy/keys/cq-target.pem root@123.57.11.86 'tar xzf - -C /opt/cq-ai-p0'

# 服务器
cd /opt/cq-ai-p0
. .venv/bin/activate
python -m pytest -q tests
systemctl restart cq-p0-api
curl -s http://127.0.0.1:8090/health
```

## 本地访问（SSH 隧道）

```bash
ssh -i .deploy/keys/cq-target.pem -N \
  -L 8090:127.0.0.1:8090 root@123.57.11.86
# 浏览器打开 http://127.0.0.1:8090/?tab=standards
```

## 跑通一个标准样例

```bash
# 服务器本地（样例 PDF 先 scp 到 /tmp）
curl -s -F "file=@/tmp/epb.pdf;filename=EPB.pdf" \
  http://127.0.0.1:8090/api/v1/standards/parse -o /tmp/epb_result.json
SID=$(python -c 'import json;print(json.load(open("/tmp/epb_result.json"))["id"])')

# 复核（按标准检验项目表口径修正项目名）
curl -s -X PATCH -H 'Content-Type: application/json' \
  -d '{"reviewer":"标准工程师","items":[{"clause":"5.3.3.1","name":"拖滞扭矩-液压拖滞扭矩"}]}' \
  http://127.0.0.1:8090/api/v1/standards/$SID > /dev/null

# 导出 / 发布 / 问答
curl -o /tmp/epb.xlsx http://127.0.0.1:8090/api/v1/standards/$SID/export.xlsx
curl -s -X POST http://127.0.0.1:8090/api/v1/standards/$SID/publish
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"question":"EPB 制动钳 5.3.11 的驻车基本性能要求是什么？","standard_id":"'$SID'"}' \
  http://127.0.0.1:8090/api/v1/standards/ask
```

解析结果与上传原件保存在 `/opt/cq-ai-p0/data/standards/<id>/`，包含保密标准，
不要复制到仓库或外发。

## Docker（待内部镜像仓库可用）

Docker Hub 当前从该服务器不可达，镜像构建暂未使用。
后续把基础镜像推到 ACR/内部仓库后，用 `docker compose up -d --build` 替换 systemd。
