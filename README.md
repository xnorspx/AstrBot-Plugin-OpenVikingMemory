# AstrBot OpenViking Memory Plugin (v1.1.5)

這是一個為 AstrBot 開發的高性能長期記憶插件，使用 [OpenViking](https://github.com/volcengine/OpenViking) 作為後端。它能讓你的機器人具備跨越會話的記憶能力，並針對複雜群聊環境與多個人格（Persona）進行了深度的感知優化與隔離設計。

## 核心特性

- **高性能連線管理 (Session Reuse)**：
  - 全面採用全局 `aiohttp.ClientSession` 管理，顯著降低頻繁 TCP/TLS 握手帶來的延遲，提升高併發場景下的穩定性。
- **環境感知型同步 (Ambient Context Sync)**：
  - **背景感知**：不僅同步 Agent 的互動，還會自動提取自上次回覆以來群組內的閒聊背景，並以 `[群聊背景上下文]` 的形式同步給 OpenViking。這解決了記憶「只有答案沒有題目」的割裂感。
- **精準記憶架構**：
  - **靜默淺層注入 (Silent Injection)**：僅在有實際歸檔或摘要時注入 `<relevant-memories>` 塊。在對話初期完全隱身，避免空標籤干擾模型判斷。
  - **深度檢索與回想 (Deep Dive)**：優化了 `memory_recall` 工具，對於搜索到的實體記憶（Level 2），插件會自動併發調用 OpenViking 文件 API 獲取**完整原文**。這確保了 UID、代碼片段等細節在摘要被壓縮時依然清晰可見。
- **人格感知 (Persona-Aware) 路由隔離**：
  - **跨平台隔離**：自動識別 Discord, QQ, 微信, 飛書 等平台，通過 `Account` 與 `User` 層級實現物理隔離。
  - **多維度 Agent 隔離**：結合 `{persona_id}` 生成唯一 `Agent ID`。群聊中同人格共享記憶，不同人格互不干擾。
- **多模態語義降級**：自動過濾並轉化媒體消息為文本標籤，維持歸檔鏈條的語義完整性。
- **單一事實源 (SSoT) 版本控制**：代碼註冊信息與日誌自動從 `metadata.yaml` 提取，確保版本標識絕對一致。

## 設計權衡 (Design Tradeoffs)

### 1. 感知深度 vs Token 開銷
- **挑戰**：群聊中大量的無關閒聊如果全量同步，會產生巨大的雜訊。
- **解決方案**：採用「觸發式背景同步」。只有當 Agent 需要開口時，才將之前的閒聊打包為 System 背景發送。這既保證了 VLM 歸檔時的語義邏輯，又節省了 80% 的 API 請求。

### 2. 摘要 vs 原文細節
- **現狀**：OpenViking 的檢索結果默認只提供摘要，容易丟失長 ID 等關鍵信息。
- **設計**：我們在插件端實施了「層次化獲取」。注入塊（L0/L1）依然使用摘要以節省 Token；而當 Agent 主動調用工具（Recall）時，插件會主動去「翻閱」L2 文件原文，實現細節的精確還原。

### 3. 指令兼容性
- **方案**：改用 `@filter.regex` 監聽 `/new`，在不干擾 AstrBot 原生重置邏輯的前提下，同步清理 OpenViking 的會話映射緩存，解決了「緩存污染」導致的 404 問題。

## 安裝與配置

1. 將本插件放入 AstrBot 的 `plugins` 目錄。
2. 在 AstrBot 管理面板的插件配置中設置：
   - **OpenViking Base URL**：你的 OpenViking 服務器地址。
   - **API Key**：可選的身份驗證密鑰。
   - **歸檔閾值**：觸發自動 Commit 的字元數上限。
3. **重要**：確保在 AstrBot 的「群聊上下文感知」中開啟 `group_icl_enable`，本插件會利用該數據源進行背景同步。

## 提供的工具 (LLM Tools)

- `memory_recall(query)`：進行全域語義檢索（跨 User/Agent 空間），自動獲取 L2 級別的詳細原文內容。
- `memory_store(fact)`：手動將重要事實存入長期記憶並立即建立索引。
- `memory_forget(uri)`：根據 URI 刪除特定的長期記憶。
- `archive_expand(archive_id)`：展開特定的歷史會話歸檔明細。
