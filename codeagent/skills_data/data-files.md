---
name: data-files
description: ملفات البيانات — JSON وCSV وYAML وقواعد البيانات
triggers: json, csv, yaml, بيانات, قاعدة بيانات, sql, تصدير, استيراد, excel
---

# ملفات البيانات

## JSON
- `ensure_ascii=False` دائماً مع العربية، وإلا صارت `\u0627\u0644`.
- `indent=2` للملفات التي يقرؤها بشر.
- تحقق دائماً بعد الكتابة: `json.loads` أو `validate_file`.

## CSV
- **اكتب BOM لملفات Excel العربية**: `encoding="utf-8-sig"`، وإلا ظهرت العربية مشوّهة.
- `newline=""` مع `csv.writer` وإلا ظهرت أسطر فارغة على ويندوز.
- الفاصلة داخل الحقول: دع `csv` يتولى الاقتباس، لا تدمج النصوص يدوياً.

## SQL
- **لا تدمج نصوصاً في استعلام أبداً.** استخدم معاملات مُهيّأة:
  ```python
  cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
  ```
- فهرس على كل عمود تبحث أو تُرتّب به.
- `TEXT` للتواريخ بصيغة ISO في SQLite، أو `TIMESTAMPTZ` في Postgres.

## التعامل مع الملفات الكبيرة
اقرأ سطراً سطراً لا دفعة واحدة:
```python
with path.open(encoding="utf-8") as f:
    for line in f:
        process(line)
```

## قائمة تحقق
- [ ] `ensure_ascii=False` في كل JSON عربي
- [ ] `utf-8-sig` في كل CSV سيُفتح بـ Excel
- [ ] لا استعلام SQL مبني بدمج نصوص
- [ ] `validate_file` بعد كل ملف بيانات
