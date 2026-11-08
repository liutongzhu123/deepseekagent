"""B7: standard-type classification (国标/行标/企标) by number and name."""
from app.standard_extract.classify import classify_standard
from app.standard_parser import parse_standard

# Curated benchmark for the three requested classes: public standard numbers and
# titles plus the enterprise numbers this project's samples use.  Expected
# labels follow the official prefix tables (GB* 国标, industry codes 行标,
# Q/ 企标).
CORPUS = [
    ("GB 5768.2-2022", "道路交通标志和标线 第2部分：道路交通标志", "national"),
    ("GB 38031-2020", "电动汽车用动力蓄电池安全要求", "national"),
    ("GB 4208-2008", "外壳防护等级（IP代码）", "national"),
    ("GB/T 228.1-2021", "金属材料 拉伸试验 第1部分：室温试验方法", "national"),
    ("GB/T 2423.1-2008", "电工电子产品环境试验 第2部分：试验方法 试验A：低温", "national"),
    ("GB/Z 24636.1-2009", "产品几何技术规范（GPS） 统计公差 第1部分：术语", "national"),
    ("GBJ 16-87", "建筑设计防火规范", "national"),
    ("GJB 150.1A-2009", "军用装备实验室环境试验方法 第1部分：通用要求", "national"),
    ("GBZ 2.1-2019", "工作场所有害因素职业接触限值 第1部分：化学有害因素", "national"),
    ("QC/T 413-2002", "汽车电气设备基本技术条件", "industry"),
    ("QC/T 29106-2014", "汽车电线束技术条件", "industry"),
    ("JB/T 4730.1-2005", "承压设备无损检测 第1部分：通用要求", "industry"),
    ("YY/T 0287-2017", "医疗器械 质量管理体系 用于法规的要求", "industry"),
    ("JGJ 26-2018", "严寒和寒冷地区居住建筑节能设计标准", "industry"),
    ("CJJ 1-2008", "城镇道路工程施工与质量验收规范", "industry"),
    ("JTG D50-2017", "公路沥青路面设计规范", "industry"),
    ("DL/T 5210.1-2012", "电力建设施工质量验收规程 第1部分：土建工程", "industry"),
    ("SJ/T 11363-2006", "电子信息产品中有毒有害物质的限量要求", "industry"),
    ("TB/T 3138-2018", "机车车辆阻燃材料技术条件", "industry"),
    ("QJ 1234-2010", "航天产品通用技术要求", "industry"),
    ("Q/JLY J7110109C-2017", "安全气囊电子控制单元及碰撞传感器技术条件", "enterprise"),
    ("Q/JLY J7110779E-2023", "乘用车电气/电子零部件电磁兼容规范", "enterprise"),
    ("Q/JLY J7111029E-2024", "汽车电气和电子零部件通用技术规范", "enterprise"),
    ("Q/JL J100003", "汽车零部件永久性标识规定", "enterprise"),
    ("Q/BYDQ 1903.001-2020", "汽车零部件技术条件", "enterprise"),
    ("QMAT 1234-2020", "企业标准试验规范", "enterprise"),
    ("QSQR 008-2019", "企业标准试验规范", "enterprise"),
    ("VW 80000-2017", "电气和电子部件通用要求", "enterprise"),
    ("Q/320115 001-2020", "企业标准技术条件", "enterprise"),
]

OTHER_LEVELS = {
    "DB11/T 1234-2020": "local", "DBJ/T 01-001-2020": "local",
    "T/CSAE 123-2020": "group", "CECS 123:2020": "group",
    "ISO 16750-2:2012": "international", "ISO/IEC 17025:2017": "international",
    "SAE J1455-2017": "international", "ASTM D638-14": "international",
    "EN 55022:2010": "international", "FMVSS 208": "international",
    "XYZ 123-2020": "unknown", "QM 1234-2020": "unknown",
}


def test_national_prefixes():
    for number in ("GB 5768.2-2022", "GB/T 228.1-2021", "GB/Z 24636.1-2009",
                   "GBJ 16-87", "GJB 150.1A-2009", "GBZ 2.1-2019"):
        result = classify_standard(number)
        assert (result["type"], result["label"]) == ("national", "国标"), number


def test_industry_prefixes_keep_the_industry_name():
    cases = {"QC/T 413-2002": "汽车", "JB/T 4730.1-2005": "机械",
             "YY/T 0287-2017": "医药", "QJ 1234-2010": "航天",
             "JGJ 26-2018": "建筑", "CJJ 1-2008": "城镇建设",
             "JTG D50-2017": "交通", "DL/T 5210.1-2012": "电力",
             "SJ/T 11363-2006": "电子", "TB/T 3138-2018": "铁道"}
    for number, industry in cases.items():
        result = classify_standard(number)
        assert (result["type"], result["subtype"]) == (
            "industry", f"{industry}行业标准"), number


def test_enterprise_prefixes():
    for number in ("Q/JLY J7110109C-2017", "Q/BYDQ 1903.001-2020",
                   "QMAT 1234-2020", "QSQR 008-2019", "QJD 12-2018",
                   "SQR 5-2019", "VW 80000-2017", "Q/320115 001-2020"):
        assert classify_standard(number)["type"] == "enterprise", number


def test_other_levels_are_not_forced_into_the_three_classes():
    for number, expected in OTHER_LEVELS.items():
        assert classify_standard(number)["type"] == expected, number


def test_number_normalisation():
    cases = {"gb/t 228.1-2021": "national",
             "ＱＣ／Ｔ ４１３－２００２": "industry",
             "GB/T228.1-2021": "national",
             "GBT 228.1-2021": "national",
             "Q／JLY J7110109C-2017": "enterprise",
             "（GB 5768.2-2022）": "national"}
    for number, expected in cases.items():
        assert classify_standard(number)["type"] == expected, number


def test_name_fallback_uses_embedded_number_then_type_word():
    assert classify_standard("", "企业标准 安全气囊电子控制单元")["type"] == "enterprise"
    assert classify_standard("XYZ 1", "GB/T 228.1-2021 金属材料 拉伸试验")["type"] == "national"
    assert classify_standard("", "Q/JLY J7110109C-2017 安全气囊")["type"] == "enterprise"
    assert classify_standard("", "Q-JLY J7110779E-2023 电磁兼容")["type"] == "enterprise"
    assert classify_standard("", "汽车电气设备基本技术条件")["type"] == "unknown"


def test_parser_meta_carries_the_type():
    result = parse_standard(["Q/TEST 123-2024\n测试标准\n2024-01-01 发布"],
                            "测试标准.pdf")
    meta = result["meta"]
    assert (meta["standard_type"], meta["standard_type_label"]) == ("enterprise", "企标")
    assert meta["standard_type_subtype"] == "企业标准（TEST）"
    assert meta["standard_type_confidence"] >= 0.9


def test_corpus_misclassification_rate_below_ten_percent():
    wrong = []
    for number, name, expected in CORPUS:
        actual = classify_standard(number, name)["type"]
        if actual != expected:
            wrong.append((number, expected, actual))
    assert len(wrong) / len(CORPUS) < 0.10, wrong
