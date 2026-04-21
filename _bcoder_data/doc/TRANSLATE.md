# /translate — language codes reference

## Modes

| Mode | Package | Model size | Quality | Internet |
|---|---|---|---|---|
| `online` | deep-translator (Google Translate) | none | High | Required |
| `mini` | argostranslate | ~100 MB per language pair | Moderate | Not required |
| `offline` | ctranslate2 + NLLB-200-distilled-600M | ~2.4 GB download, ~600 MB after conversion | Good | Not required |
| `lm` | Ollama / LM Studio | depends on model | High | Not required |

`online` — best quality, instant, requires internet. Not for confidential content.
`mini` — smallest footprint, suitable for low-RAM devices (Termux, air-gapped).
`offline` — best local quality, runs in-process (no model switching overhead in Ollama).
`lm` — use when you have a dedicated translation model already loaded.

## Setup

```
/translate setup lang:uk mode:online
/translate setup lang:uk mode:mini    # downloads ~100MB argostranslate packages
/translate setup lang:uk mode:offline # downloads ~2.4GB, converts to ~600MB int8 (one-time)
/translate setup lang:uk mode:lm host:192.168.0.5:11434 model:translategemma:4b
```

## Language codes — online mode (Google Translate)

### European languages

| Code | Language |
|---|---|
| `af` | Afrikaans |
| `be` | Belarusian |
| `bg` | Bulgarian |
| `bs` | Bosnian |
| `ca` | Catalan |
| `cs` | Czech |
| `cy` | Welsh |
| `da` | Danish |
| `de` | German |
| `el` | Greek |
| `en` | English |
| `eo` | Esperanto |
| `es` | Spanish |
| `et` | Estonian |
| `eu` | Basque |
| `fi` | Finnish |
| `fr` | French |
| `ga` | Irish |
| `gl` | Galician |
| `hr` | Croatian |
| `hu` | Hungarian |
| `hy` | Armenian |
| `is` | Icelandic |
| `it` | Italian |
| `ka` | Georgian |
| `lt` | Lithuanian |
| `lv` | Latvian |
| `mk` | Macedonian |
| `mt` | Maltese |
| `nl` | Dutch |
| `no` | Norwegian |
| `pl` | Polish |
| `pt` | Portuguese |
| `ro` | Romanian |
| `ru` | Russian |
| `sk` | Slovak |
| `sl` | Slovenian |
| `sq` | Albanian |
| `sr` | Serbian |
| `sv` | Swedish |
| `tr` | Turkish |
| `uk` | Ukrainian |

### Asian languages

| Code | Language |
|---|---|
| `az` | Azerbaijani |
| `bn` | Bengali |
| `gu` | Gujarati |
| `hi` | Hindi |
| `id` | Indonesian |
| `ja` | Japanese |
| `jw` | Javanese |
| `kk` | Kazakh |
| `km` | Khmer |
| `kn` | Kannada |
| `ko` | Korean |
| `ky` | Kyrgyz |
| `lo` | Lao |
| `ml` | Malayalam |
| `mn` | Mongolian |
| `mr` | Marathi |
| `ms` | Malay |
| `my` | Myanmar (Burmese) |
| `ne` | Nepali |
| `pa` | Punjabi |
| `ps` | Pashto |
| `sd` | Sindhi |
| `si` | Sinhala |
| `ta` | Tamil |
| `te` | Telugu |
| `tg` | Tajik |
| `th` | Thai |
| `tl` | Filipino |
| `ur` | Urdu |
| `uz` | Uzbek |
| `vi` | Vietnamese |
| `zh-CN` | Chinese (Simplified) |
| `zh-TW` | Chinese (Traditional) |

### Middle Eastern languages

| Code | Language |
|---|---|
| `ar` | Arabic |
| `fa` | Persian (Farsi) |
| `he` | Hebrew |
| `ku` | Kurdish |

### African languages

| Code | Language |
|---|---|
| `am` | Amharic |
| `ha` | Hausa |
| `ig` | Igbo |
| `mg` | Malagasy |
| `ny` | Chichewa |
| `sm` | Samoan |
| `sn` | Shona |
| `so` | Somali |
| `st` | Sesotho |
| `sw` | Swahili |
| `xh` | Xhosa |
| `yo` | Yoruba |
| `zu` | Zulu |

### Other

| Code | Language |
|---|---|
| `co` | Corsican |
| `fy` | Frisian |
| `ht` | Haitian Creole |
| `la` | Latin |
| `lb` | Luxembourgish |
| `mi` | Maori |
| `su` | Sundanese |

## Language codes — offline mode (argostranslate)

Argostranslate supports a smaller set. Available pairs depend on downloaded packages.
Commonly available: `ar`, `az`, `ca`, `cs`, `da`, `de`, `el`, `en`, `eo`, `es`, `fa`,
`fi`, `fr`, `ga`, `gl`, `he`, `hi`, `hr`, `hu`, `id`, `it`, `ja`, `ko`, `lt`, `lv`,
`ms`, `nl`, `no`, `pl`, `pt`, `ro`, `ru`, `sk`, `sl`, `sq`, `sr`, `sv`, `th`, `tl`,
`tr`, `uk`, `vi`, `zh`.

All translations go through English as pivot language (e.g. `uk → en → de`).
For offline, only `uk → en` and `en → uk` packages are needed — the pivot is automatic.

## Examples

```
/translate setup uk online
/translate setup hi online
/translate setup sw online
/translate last
/translate last lang de
/translate last mode offline lang fr
/translate mode offline
/translate off
```
