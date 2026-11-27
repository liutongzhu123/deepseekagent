# CNZ WeKnora 合成验收素材

本目录只含**虚构演示数据**，不代表客户资料、真实标准、报价规则或资质结论。不得用于生产报价、客户答复或业务验收。

## 文件

- `Q-DEMO-001-2026.md`：可上传到隔离 CNZ 测试知识库的 Markdown 源文件，含 6 个虚构条款及 6 个方法段。
- `../../../p0/eval/weknora_qa.demo.json`：同一组数据的 22 条问答集，14 条预期命中、8 条预期拒答。

## 后续测试步骤

获得合成测试管理员账号后，在 CNZ WeKnora 创建临时测试知识库，只上传本目录的 Markdown 文件；不要把它上传到 `yunan-persist` 或生产知识库。等待解析/索引完成，再使用受控运行环境中的 WeKnora URL、服务密钥和测试知识库 ID 运行：

```sh
python p0/scripts/run_weknora_eval.py \
  --dataset p0/eval/weknora_qa.demo.json \
  --mode weknora \
  --strict \
  --report /private/tmp/cnz-weknora-live-eval.json \
  --markdown /private/tmp/cnz-weknora-live-eval.md
```

运行环境通过已批准的私密配置提供 `P0_WEKNORA_BASE_URL`、`P0_WEKNORA_API_KEY`、`P0_WEKNORA_KB_ID`；这些值不能写入仓库或评测报告。确认评测输出中的实际模式是 `weknora`，并记录命中、拒答、引用准确度与延迟。逐条复核 14 个命中用例的标准号和条款号、8 个库外用例均无引用；对解析失败、空检索、错误引用和服务异常分别记录，不允许静默回退成关键词模式后把评测算作 WeKnora 通过。

验收完成后删除临时测试知识库及其中的合成资料，并回读确认已删除。2026-11-27 已注册并只读回读确认 testkb 存在 1 个系统管理员账号；知识库、知识、资源、分块和消息仍为 0，尚未创建临时测试知识库。所以上传、解析、索引、检索、引用和删除步骤仍待执行；现有关键词评测结果不能替代这些步骤。运行时核验未读取或保存管理员凭据。
