# WeKnora Q&A 评测：P1-3 WeKnora Q&A demo baseline

- 模式：`keyword`；知识库 1 份 / 12 片段
- 用例：22
- 库内命中：9/14（64.3%）
- 库外拒答：8/8（100.0%）
- 引用准确：64.3%
- 延迟：平均 0.1 ms / P95 0.1 ms

| 标签 | 正确/总数 | 准确率 |
| --- | --- | --- |
| direct | 6/6 | 100.0% |
| out_of_kb | 8/8 | 100.0% |
| paraphrase | 3/8 | 37.5% |

## 未通过用例
- `q08` hit / paraphrase：0.2 MPa 保压多久不能泄漏？（found=False，citation_ok=False，error=）
- `q09` hit / paraphrase：水平燃烧速度限值是多少？（found=False，citation_ok=False，error=）
- `q10` hit / paraphrase：汽油浸泡后不能出现什么？（found=False，citation_ok=False，error=）
- `q11` hit / paraphrase：雾化值要求是多少？（found=False，citation_ok=False，error=）
- `q12` hit / paraphrase：插拔力范围是多少？（found=False，citation_ok=False，error=）
