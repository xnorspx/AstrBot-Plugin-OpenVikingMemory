# AstrBot OpenViking Memory Plugin

這是一個為 AstrBot 開發的長期記憶插件，使用 [OpenViking](https://github.com/volcengine/OpenViking) 作為後端。它能讓你的機器人具備跨越會話的記憶能力，並針對不同類型的對話場景進行了自動化的路由與隔離優化。

## 核心特性

- **雙層記憶架構**：
  - **淺層直覺 (Shallow Intuition)**：在每次 LLM 請求前，自動從 OpenViking 檢索最相關的歸檔摘要（限制在 800 Tokens 內），讓機器人「天生」具備連貫的上下文感。
  - **深層回想 (Deep Recall)**：為 Agent 提供主動工具（`memory_recall`, `archive_expand`），當淺層記憶不足以回答問題時，Agent 可以主動翻找深層檔案。
- **統一路由隔離公式**：
  - **跨平台支持**：自動識別 Discord, QQ, 微信, 飛書, Webchat 等平台。
  - **Account 層級**：`astrbot_{platform}`（例如 `astrbot_discord`），實現平台間物理隔離。
  - **User 層級**：`{platform}_{user_id}`，確保用戶身份全球唯一。
  - **Agent 隔離邏輯**：
    - **群聊模式**：路由至 `agent_{platform}_group_{id}`，實現伺服器成員間的集體記憶共享。
    - **私聊模式**：路由至 `agent_{platform}_user_{id}`，確保用戶專屬的私密對話空間。
- **多模態降級 (Multi-modal Fallback)**：自動將圖片、語音、影片等消息轉換為文本佔位符，確保 OpenViking 的文本歸檔流程不被中斷，並利用 LLM 的後續回覆進行語義補完。
- **會話同步與重置**：採用正則監聽模式捕捉 `/new` 指令，確保與 AstrBot 內核兼容的同時，同步重置 OpenViking 的會話映射。

## 設計權衡 (Design Tradeoffs)

### 1. 記憶注入策略：淺層 vs 深層
- **挑戰**：全量注入記憶會導致 Token 費用飆升，且會污染當前對話。
- **解決方案**：採用「自動淺層注入 + 主動工具翻找」的組合拳。淺層注入保證了基礎的「熟悉感」，而深度信息則交給 Agent 根據需求主動回想。

### 2. 統一標識符與 Agent 共享
- **現狀**：OpenViking 的隔離核心在於 Agent ID。
- **設計**：我們將「私聊」與「群聊」的隔離邏輯統一化。在群聊中，同一伺服器使用同一個 Agent ID 以支持「群體記憶」；在私聊中，Agent ID 與 User ID 綁定以實現「個人隱私」。

### 3. 會話重置與指令衝突
- **挑戰**：AstrBot 內核已佔用 `/new` 指令，插件註冊同名指令會導致衝突。
- **解決方案**：插件改用 `@filter.regex` 進行被動監聽。當用戶輸入 `/new` 時，插件會先切換 OpenViking 的 Session，隨後讓內核繼續執行其原有的上下文清理工作。

### 4. 多模態內容的語義鏈條
- **現狀**：OpenViking 依賴文本進行記憶提煉。
- **設計**：我們將媒體轉換為 `[圖片消息]` 等標籤。因為 Agent 隨後的文字回覆（例如「這張風景照真漂亮」）已經為歸檔提供了足夠的語義信息，讓 OpenViking 後台 VLM 能夠「理解」對話背景。

### 5. 測試環境保護
- **設計**：插件會自動過濾來自 `dashboard` (WebUI 內部的調試對話) 的消息存儲，防止臨時測試數據污染正式的長期記憶庫。但 **Web Chat** 通道會被正常記錄。

## 安裝與配置

1. 將本插件放入 AstrBot 的 `plugins` 目錄。
2. 在 AstrBot 管理面板的插件配置中設置：
   - **OpenViking Base URL**：你的 OpenViking 服務器地址（預設 `http://127.0.0.1:1933`）。
   - **API Key**：可選的身份驗證密鑰。
   - **歸檔閾值**：觸發自動 Commit 的字元數上限（預設 2000）。
3. 在 OpenViking 的 `ov.yaml`（或配置文件）中建議設置 `memory.agent_scope_mode: "agent"` 以啟用群聊共享能力。你可以參考插件目錄下的 `ov.conf.template` 進行配置。

## 提供的工具 (LLM Tools)

- `memory_recall(query)`：進行全局語義檢索，返回相關記憶片段及 URI。
- `memory_store(fact)`：手動將重要事實存入長期記憶並立即建立索引。
- `memory_forget(uri)`：根據 URI 刪除特定的長期記憶。
- `archive_expand(archive_id)`：展開特定的歷史歸檔明細以查看原始對話。
