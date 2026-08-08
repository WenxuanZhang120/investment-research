# 配置文件说明

本目录保存稳定、非敏感且需要版本控制的项目规则。账号、密码、令牌和其他凭据不得写入这里。

## `project.toml`

保存项目名称、时区、目录路径、原始数据保护原则和数据血缘字段要求。

## `field_mappings.json`

保存 iWencai 原始基础字段名到标准字段名的映射。映射文件使用 JSON，以便当前 Python 3.8 环境直接通过标准库读取，不需要安装第三方 TOML 或 YAML 解析器。

每项映射包括：

- `canonical_field_name`：系统内部使用的稳定字段名；
- `category`：字段类别；
- `raw_base_fields`：iWencai 可能返回的原始基础字段名列表。

示例：

```json
{
  "canonical_field_name": "close",
  "category": "market",
  "raw_base_fields": ["收盘价"]
}
```

## 修改规则

1. 原始字段名必须精确保留，不得先翻译或改写。
2. 新增或调整映射时必须更新 `mapping_version`。
3. 一个原始基础字段只能映射到一个标准字段。
4. 不确定的字段不得猜测映射，应保留为 `unmapped`。
5. 单位和复权口径只有在来源明确时才能加入映射。
6. 每次修改映射都必须增加对应的自动测试。

## 检查字段解析结果

解析器只处理字段名称，不读取或修改数据值，也不会发起网络请求：

```bash
python3 scripts/parse_iwencai_fields.py \
  "收盘价[20260807]" \
  "归母净利润[2026一季报]" \
  --pretty
```

输出会同时保留 `raw_field_name`，并记录标准字段名、日期或报告期、映射版本、解析器版本和置信度。无法识别的字段会标记为 `unmapped`。
