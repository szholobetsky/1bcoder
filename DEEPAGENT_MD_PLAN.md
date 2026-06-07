# deepagent_md — план покращень

## Проблема

`--web` у поточній реалізації бере перші 2000 символів кожної сторінки.
Для наукових статей і дисертацій це дає мотиваційну воду зі вступу.
Корисний контент — в середині або в кінці.
Результат: deepagent_md генерує красиво оформлену воду замість аналізу.

---

## Пайплайн

```
SEARCH → FETCH → FILTER → COMPACT → GENERATE
```

Модель задіяна тільки на FILTER (YES/NO), COMPACT (стисни) і GENERATE (напиши).
Всі інші кроки — детермінований код.

---

## Ітерація 1 — Контекст і посилання

### `--fix top:N,mid:N,last:N`

Вирізати конкретні частини тексту замість завжди `top:2000`.

```
--fix top:2000                       # як зараз (дефолт якщо нічого не задано)
--fix top:1000,mid:1000              # початок + середина
--fix top:1000,mid:500,last:500      # три шматки, з'єднані через "[...]"
```

`mid:N` = N символів з центру тексту.

---

### `--scan N`

Компактне читання великих документів.

```
--scan 200
```

Логіка:
1. `chunk_size = num_ctx * 50% * 4` (символів; для ctx=2048 → ~4096 chars на chunk)
2. Розбити source text на chunks по chunk_size
3. Для кожного chunk: окремий LLM виклик → стиснути до N символів
4. Склеїти compacted pieces
5. В генерацію йде ТІЛЬКИ compacted текст, ніколи сирий

Промпт compact:
```
Summarize in {N} characters or less. Preserve key facts, numbers, names,
technical details. Output ONLY the summary.

{chunk}
```

`--scan` і `--fix` взаємовиключні. `--scan` має пріоритет.
Дефолт якщо нічого: `--fix top:2000`.

---

### `--ref` (під час генерації)

Збирати посилання в `plan_dir/refs.json`.
Формат: `[{"node": "1.2", "title": "...", "url": "..."}, ...]`

---

### `compose --ref all | --ref distinct`

```
/flow deepagent_md compose plan1 --ref all
/flow deepagent_md compose plan1 --ref distinct
```

**`--ref all`** — після контенту кожного вузла:
```markdown
### References
1. Title — URL
2. Title — URL
```

**`--ref distinct`** — один глобальний список в кінці документа,
дедуплікований за URL:
```markdown
## References
1. Title — URL
2. Title — URL
```

---

## Ітерація 2 — Фільтрація і пресети

### `--prescan`

Перед тим як включати джерело в контекст — запит до LLM:

```
Does this text address "{section_title}" in the context of "{root_task}"?
Reply YES or NO only.
```

NO → джерело пропускається. Дешево навіть для 0.5b моделей.

---

### `--preset quick | balanced | deep`

Назва `--preset` (не `--mode` — той зайнятий в compose, не `--profile` — зайнятий workers).

```
--preset quick      →  --web 3  --fix top:2000
--preset balanced   →  --web 5  --scan 200
--preset deep       →  --web 10 --scan 200 --prescan --ref
```

Явно задані параметри overrideять preset.

---

### `--web N` (upgrade)

`--web` без числа = `--web 3` (як зараз).
`--web 5` = явно задати кількість сторінок.
Тепер це число реально передається в `_web_research`.

---

### `webanalys` — окремий flow

```
/flow webanalys "Cauchy problem heat equation Java"
```

По суті: prescan у видимому інтерактивному режимі.
Повертає N джерел з оцінкою 0-5 і коментарем:

```
1. [4/5] "Solving PDEs in Java — TU Munich"
         URL: https://...
         Why: Contains actual Java code for finite difference method

2. [2/5] "Heat Transfer Fundamentals"
         URL: https://...
         Why: Theory only, no implementation

3. [0/5] "Java Spring Boot Tutorial"
         URL: https://...
         Why: Unrelated to PDEs
```

Корисно перед довгим `deepagent_md --preset deep` щоб зрозуміти
яку якість джерел дає запит.

---

## Ітерація 3 — RAG і webindex

### `--rag project:X path:P`

```
--rag project:default path:C:\MyProject\
```

1. Виклик `simargl search "{title} {root_task}"` з `cwd=path`
2. Беремо `text_preview` з task results (повний, не обрізаний до 80)
3. Для file results — читаємо файли з диска
4. Якщо знайдено > threshold символів → web не запускається
5. `--rag` + `--web` разом: rag першим, web тільки якщо rag порожній

simargl search повертає:
- task mode: `{"unit_id", "text_preview", "similarity", "files"}` — text є
- file mode: `{"path", "score", "module"}` — текст читати з диска

---

### `webindex` — окремий flow

Краулер + simargl індексація в один крок:

```
/flow webindex https://docs.example.com --project myproject --path C:\MyProject\ --depth 3
```

1. BFS краулер (webcrawl логіка), `--depth` рівнів
2. Зберігає сторінки як `.txt` в `path/.simargl_web/`
3. Запускає `simargl index` на цій папці
4. Після цього `--rag project:myproject path:C:\MyProject\` знайде веб-контент

---

## Стан реалізації

- [x] Ітерація 1: `--fix`, `--scan`, `--ref`, `compose --ref all|distinct`, `--preset`, `--prescan`, `--web N`
- [x] Ітерація 2: `webanalys` flow (`_bcoder_data/flows/webanalys.py`)
- [x] Ітерація 3: `--rag project:X path:P`, `webindex` flow (`_bcoder_data/flows/webindex.py`)
