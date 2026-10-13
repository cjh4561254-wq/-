"""数据层（app/data/）：把"用户上传的一个文件"变成"程序里能分析的一份数据"。

这个包下面三个模块，各管一段，边界不要混：

    loader.py    读文件  —— 编码/分隔符兜底、只读第一个 sheet、最小清洗、卡行列上限
    store.py     记数据  —— 进程内字典，用 dataset_id 把 DataFrame 和元信息对上号
    profiler.py  看数据  —— 用 pandas 真实算出画像（缺失、唯一值、五数概括……）

三者之间没有互相 import：路由层按 loader → store → profiler 的顺序调用它们。
这样做的好处是每个模块都能单独测试，也能单独讲清楚"它负责什么、不负责什么"。

为什么 DataFrame 不放进 Pydantic 模型、也不在模块之间传来传去？
    见 app/schemas_dataset.py 顶部的说明：DataFrame 只放在 store 的进程内字典里，
    其他模块拿 dataset_id 去取。
"""
