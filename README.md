# AstrBot OpenViking Memory Plugin

這是一個為 AstrBot 開發的長期記憶插件，使用 [OpenViking](https://github.com/volcengine/OpenViking) 作為後端。它能讓你的機器人具備跨越會話的記憶能力，並針對不同類型的對話場景與多個人格（Persona）進行了自動化的路由與強隔離優化。

## 核心特性

- **雙層記憶架構**：
  - **淺層直覺 (Shallow Intuition)**：在每次 LLM 請求前，自動從 OpenViking 檢索最相關的歸檔摘要與提煉後的長效記憶（限制在 800 Tokens 內），讓機器人「天生」具備連貫的上下文感。
  - **深層回想 (Deep Recall)**：為 Agent 提供主動工具（`memory_recall`, `archive_expand`），當淺層記憶不足以回答問題時，Agent 可以主動翻找深層檔案。
- **人格感知 (Persona-Aware) 路由隔離公式**：
  - **跨平台支持**：自動識別 Discord, QQ, 微信, 飛書, Webchat 等平台。
  - **Account 層級**：`astrbot_{platform}`（例如 `astrbot_discord`），實現平台間物理隔離。
  - **User 層級**：`{platform}_{user_id}`，確保用戶身份全球唯一。
  - **Agent 隔離邏輯 (Bot 多人格支持)**：
    - **群聊模式**：路由至 `agent_{platform}_{persona_id}_group_{id}`。這確保了同一個群組內，不同人格（Bot）擁有獨立的記憶空間，而同一個人格在該群組內共享集體記憶。
    - **私聊模式**：路由至 `agent_{platform}_{persona_id}_user_{id}`，確保用戶與該特定人格間的私密對話空間。
- **多模態降級 (Multi-modal Fallback)**：自動將圖片、語音、影片等消息轉換為文本佔位符，確保 OpenViking 的文本歸檔流程不被中斷，並利用 LLM 的後續回覆進行語義補完。
- **會話同步與重置**：採用正則監聽模式捕捉 `/new` 指令，確保與 AstrBot 內核兼容的同時，同步重置 OpenViking 的會話映射。

## 設計權衡 (Design Tradeoffs)

### 1. 記憶注入策略：淺層 vs 深層
- **挑戰**：全量注入記憶會導致 Token 費用飆升，且會污染當前對話。
- **解決方案**：採用「自動淺層注入 + 主動工具翻找」的組合拳。淺層注入包含「近期歸檔摘要」與「長效事實經驗」，保證基礎熟悉感；深度信息交給 Agent 主動回想。

### 2. 多人格（Persona）下的記憶防撞
- **現狀**：同一個用戶或群組可能會與不同的人格對話。
- **設計**：我們在 `Agent ID` 中強行引入了 `{persona_id}`。這意味著 OpenViking 會為每一個「人格 + 對話環境」對象建立一個獨立的「大腦」，徹底避免了不同 Bot 之間記憶串聯的風險。

### 3. 會話重置與指令衝突
- **挑戰**：AstrBot 內核已佔用 `/new` 指令，插件註冊同名指令會導致衝突。
- **解決方案**：插件改用 `@filter.regex` 進行被動監聽。當用戶輸入 `/new` 時，插件會先切換 OpenViking 的 Session，隨後讓內核繼續執行其原有的上下文清理工作。

### 4. 多模態內容的語義鏈條
- **設計**：我們將媒體轉換為 `[圖片消息]` 等標籤。因為 Agent 隨後的文字回覆已經為歸檔提供了足夠的語義信息，讓 OpenViking 後台 VLM 能夠在提煉時理解上下文。

## 安裝與配置

1. 將本插件放入 AstrBot 的 `plugins` 目錄。
2. 在 AstrBot 管理面板的插件配置中設置：
   - **OpenViking Base URL**：你的 OpenViking 服務器地址。
   - **API Key**：可選的身份驗證密鑰。
   - **歸檔閾值**：觸發自動 Commit 的字元數上限。
3. **重要**：在 AstrBot 的 Persona (人格/角色) 設置中，確保為目標 Agent 啟用了本插件。
4. 在 OpenViking 的 `ov.yaml` 中建議設置 `memory.agent_scope_mode: "agent"`。

## 提供的工具 (LLM Tools)

- `memory_recall(query)`：進行全局語義檢索，返回相關記憶片段及 URI。
- `memory_store(fact)`：手動將重要事實存入長期記憶並立即建立索引。
- `memory_forget(uri)`：根據 URI 刪除特定的長期記憶。
- `archive_expand(archive_id)`：展開特定的歷史歸檔明細。
