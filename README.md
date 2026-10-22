# 記憶分層壓縮

> 給 LivingMemory 做記憶分層：零碎記憶按日、周、月、季逐級壓縮，省 token

AstrBot 插件，屬於「擬真群友」（lively）插件套件。適用 AstrBot >=4.29.0 + NapCat（aiocqhttp）。

主人、群、機器人名字、人設用詞這些共用設定在 [astrbot_plugin_lively_gate](https://github.com/GUCine/astrbot_plugin_lively_gate) 裡填，建議一起裝。

## 功能

```text
機器人的記憶分層：日 → 周 → 月 → 季。

LivingMemory 每聊一段就寫一條記憶（一天幾十條），這裡每天 RUN_AT 把它們逐級壓縮：
- 日：前一天同一個群（或同一個人的私聊）的零碎記憶合成幾條「日記憶」，保留 7 天
- 周：超過 7 天的日記憶按自然周合成「週記憶」，保留 4 周
- 月：超過 4 周的週記憶按月合成「月記憶」，保留 3 個月
- 季：超過 3 個月的月記憶按季度合成「季記憶」，長期保留
同一時段後來又冒出低一級的記憶（比如晚到的一段聊天），會和已有的那條重新合併
重要度 >= PROTECT 的記憶（生日、約定、主人交代的事）原樣保留，不參與壓縮
被合併的舊記憶只是歸檔（不再參與召回），文檔還在，可以恢復
只通過 LivingMemory 自己的 add_memory / archive_memories 寫入，不改它的程式碼
主人私信指令：記憶分層 狀態 / 現在 / 開 / 關
```

## 依賴

[LivingMemory](https://github.com/lxfight/astrbot_plugin_livingmemory)（必需）。

## 配置

在 AstrBot WebUI 的插件配置裡填，或者直接編輯 `data/config/astrbot_plugin_lively_memtier_config.json`。留空的公共項（主人、群、機器人名字、平台）會用 astrbot_plugin_lively_gate 裡填的。

| 配置項 | 說明 | 預設 |
| --- | --- | --- |
| `provider_id` | 壓縮記憶用的模型：留空用 AstrBot 預設模型 | （空） |
| `owner_qq` | 主人 QQ（留空用門禁插件裡填的） | （空） |

## 資料

執行資料在 `data/plugin_data/astrbot_plugin_lively_memtier/`。

## 許可

MIT，見 [LICENSE](LICENSE)。
