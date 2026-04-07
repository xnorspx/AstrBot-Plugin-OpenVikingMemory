# AstrBot OpenViking Memory Plugin

這是一個為 AstrBot 開發的長期記憶插件，使用 [OpenViking](https://github.com/volcengine/OpenViking) 作為後端。它能讓你的機器人具備跨越會話的記憶能力，並針對不同類型的 Agent 進行了專門的路由優化。

## 核心特性

- **雙層記憶架構**：
  - **淺層直覺 (Shallow Intuition)**：在每次 LLM 請求前，自動從 OpenViking 檢索最相關的歸檔摘要（限制在 800 Tokens 內），讓機器人「天生」具備連貫的上下文感。
  - **深層回想 (Deep Recall)**：為 Agent 提供主動工具（`memory_recall`, `archive_expand`），當淺層記憶不足以回答問題時，Agent 可以主動翻找深層檔案。
- **全平台適配與路由隔離**：
  - **跨平台支持**：自動識別 Discord, QQ (OneBot), 微信, 飛書等平台。
  - **個人助手 (Personal AI)**：記憶嚴格隔離在用戶個人空間。
  - **群聊/伺服器共享記憶**：在群聊中，同一伺服器內的成員共享「伺服器文化」記憶，但不同伺服器間完全隔離。
- **多模態降級 (Multi-modal Fallback)**：自動將圖片、語音、影片等消息轉換為文本佔位符，確保 OpenViking 的文本歸檔流程不被中斷，並利用 LLM 的後續回覆進行語義補完。
- **會話同步與重置**：採用正則監聽模式捕捉 `/new` 指令，確保與 AstrBot 內核兼容的同時，同步重置 OpenViking 的會話映射。

## 設計權衡 (Design Tradeoffs)

### 1. 記憶注入策略：淺層 vs 深層
- **挑戰**：全量注入記憶會導致 Token 費用飆升，且會污染當前對話。
- **解決方案**：採用「自動淺層注入 + 主動工具翻找」的組合拳。淺層注入保證了基礎的「熟悉感」，而深度信息則交給 Agent 根據需求主動回想。

### 2. 會話重置與指令衝突
- **挑戰**：AstrBot 內核已佔用 `/new` 指令，插件註冊同名指令會導致衝突。
- **解決方案**：插件改用 `@filter.regex` 進行被動監聽。當用戶輸入 `/new` 時，插件會先切換 OpenViking 的 Session，隨後讓內核繼續執行其原有的上下文清理工作。

### 3. 多模態內容的語義鏈條
- **現狀**：OpenViking 依賴文本進行記憶提煉。
- **設計**：我們沒有在插件層重新運行 VLM 解析媒體，而是將媒體轉換為 `[圖片消息]` 等標籤。因為 Agent 隨後的文字回覆已經為歸檔提供了足夠的語義信息，讓後台 VLM 能夠在總結時「理解」發生了什麼。

### 4. 平台與測試環境過濾
- **平台無關性**：路由 Header (X-OpenViking-Account) 使用 `{platform}_{id}` 格式，確保了在多平台同時掛載時，記憶空間不會混淆。
- **測試環境保護**：插件會自動識別並過濾來自 `dashboard` (WebUI 測試對話) 的消息存儲，防止測試數據污染長期記憶庫。

### 5. Token 計算策略
- **估算法**：插件使用「字元數 (Character Count)」來估算對話長度。預設累積 2000 字元觸發一次 Commit。
- **理由**：這避免了引入重量級的 Tokenizer 依賴（如 tiktoken），在保證輕量化的同時，為 OpenViking 提供了足夠密度的信息進行提煉。

## 安裝與配置

1. 將本插件放入 AstrBot 的 `plugins` 目錄。
2. 在 AstrBot 管理面板的插件配置中設置：
   - **OpenViking Base URL**：你的 OpenViking 服務器地址。
   - **API Key**：可選的身份驗證密鑰。
   - **歸檔閾值**：觸發自動 Commit 的字元數上限。
3. 在 OpenViking 的配置中建議設置 `memory.agent_scope_mode: "agent"`。

## 提供的工具 (LLM Tools)

- `memory_recall(query)`：進行全局語義檢索。
- `memory_store(fact)`：手動存入重要事實並立即建立索引。
- `archive_expand(archive_id)`：展開特定的歷史歸檔明細。
