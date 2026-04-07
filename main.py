import aiohttp
import asyncio
import hashlib
import time
import uuid
from typing import Dict, List, Optional

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    At,
    AtAll,
    BaseMessageComponent,
    Face,
    File,
    Forward,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register("openviking_memory", "Sunny", "OpenViking Long-term Memory Plugin", "1.0.0")
class OpenVikingMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config
        
        # Local mapping: unified_msg_origin -> OV_session_id
        # In a production environment, this should be in self.context.db
        self.session_map: Dict[str, str] = {}
        
        self.pending_tokens: Dict[str, int] = {}

    async def initialize(self):
        """Initialize plugin."""
        logger.info("OpenViking Memory Plugin initialized.")

    def _get_ov_base_url(self) -> str:
        return self.config.get("ov_base_url", "http://127.0.0.1:1933").rstrip("/")

    def _get_api_key(self) -> str:
        return self.config.get("api_key", "")

    def _get_commit_threshold(self) -> int:
        return self.config.get("commit_threshold", 2000)

    def _get_headers(self, event: AstrMessageEvent) -> Dict[str, str]:
        """Construct routing headers with a unified, platform-safe naming convention."""
        platform = event.get_platform_name()
        group_id = event.get_group_id() 
        user_id = event.get_sender_id()
        
        # Consistent Account ID per platform
        account_id = f"astrbot_{platform}"
        
        # User ID is always platform-scoped user
        ov_user_id = f"{platform}_{user_id}"
        
        # Agent ID determines the memory scope (Shared for groups, Private for users)
        if group_id:
            # Shared memory for the entire Group/Server
            agent_id = f"agent_{platform}_group_{group_id}"
        else:
            # Dedicated memory for the individual User
            agent_id = f"agent_{platform}_user_{user_id}"

        headers = {
            "X-OpenViking-Account": account_id,
            "X-OpenViking-User": ov_user_id,
            "X-OpenViking-Agent": agent_id,
            "Content-Type": "application/json",
        }
        api_key = self._get_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def _get_ov_session(self, event: AstrMessageEvent) -> str:
        """Get or create OpenViking session ID for the current AstrBot session."""
        umo = event.unified_msg_origin
        if umo not in self.session_map:
            # Create new session in OV
            headers = self._get_headers(event)
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self._get_ov_base_url()}/api/v1/sessions", headers=headers, json={}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.session_map[umo] = data["result"]["session_id"]
                    else:
                        # Fallback or error
                        logger.error(f"Failed to create OV session: {await resp.text()}")
                        # Use UMO hash as fallback to at least have something
                        self.session_map[umo] = hashlib.md5(umo.encode()).hexdigest()
        return self.session_map[umo]

    def _degrade_message(self, chain: List[BaseMessageComponent]) -> str:
        """Degrade multi-modal components into text placeholders."""
        parts = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(comp.text)
            elif isinstance(comp, Image):
                parts.append("[圖片消息]")
            elif isinstance(comp, Record):
                parts.append("[語音消息]")
            elif isinstance(comp, Video):
                parts.append("[影片消息]")
            elif isinstance(comp, File):
                parts.append(f"[文件: {comp.name}]")
            elif isinstance(comp, Face):
                parts.append(f"[表情: {comp.id}]")
            elif isinstance(comp, At):
                parts.append(f"[@{comp.qq}]")
            elif isinstance(comp, AtAll):
                parts.append("[@全體成員]")
            elif isinstance(comp, Reply):
                parts.append(f"[引用回覆: {comp.message_str}]")
            else:
                parts.append(f"[{type(comp).__name__}]")
        return "".join(parts).strip()

    @filter.on_llm_request()
    async def before_llm_request(self, event: AstrMessageEvent, request: ProviderRequest):
        """Inject relevant memories into the prompt (Shallow Intuition)."""
        if event.get_platform_name() == "dashboard":
            return
        try:
            ov_session_id = await self._get_ov_session(event)
            headers = self._get_headers(event)
            
            async with aiohttp.ClientSession() as session:
                # 1. Fetch relevant memories (Top-K)
                # Budget is limited to ~800 tokens. OV context endpoint provides archives + messages
                async with session.get(
                    f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/context?token_budget=800",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        ctx_data = await resp.json()
                        ov_ctx = ctx_data.get("result", {})
                        
                        # Format memories for prompt injection
                        memory_block = "\n<relevant-memories>\n"
                        if "archives" in ov_ctx and ov_ctx["archives"]:
                            memory_block += "[歷史歸檔摘要]:\n"
                            for arch in ov_ctx["archives"]:
                                memory_block += f"- {arch.get('overview', '')}\n"
                        
                        # Add a hint about deep recall if needed
                        memory_block += "\n(如果你需要獲取更詳細的信息，可以調用 memory_recall 工具。)\n"
                        memory_block += "</relevant-memories>\n"
                        
                        # Prepend to system prompt or user prompt
                        if request.system_prompt:
                            request.system_prompt = memory_block + request.system_prompt
                        else:
                            request.prompt = memory_block + request.prompt
                            
        except Exception as e:
            logger.error(f"Error in OpenViking Shallow Intuition: {e}")

    @filter.on_llm_response()
    async def after_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        """Sync the conversation turn to OpenViking."""
        if event.get_platform_name() == "dashboard":
            return
        try:
            ov_session_id = await self._get_ov_session(event)
            headers = self._get_headers(event)
            
            user_text = self._degrade_message(event.get_messages())
            assistant_text = response.completion_text if response.completion_text else "[Tool Call/Empty Response]"
            
            async with aiohttp.ClientSession() as session:
                # 1. Add User Message
                await session.post(
                    f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/messages",
                    headers=headers,
                    json={"role": "user", "content": user_text}
                )
                
                # 2. Add Assistant Message
                await session.post(
                    f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/messages",
                    headers=headers,
                    json={"role": "assistant", "content": assistant_text}
                )
                
                # 3. Handle Auto-Commit
                umo = event.unified_msg_origin
                self.pending_tokens[umo] = self.pending_tokens.get(umo, 0) + len(user_text) + len(assistant_text)
                
                if self.pending_tokens[umo] >= self._get_commit_threshold():
                    logger.info(f"Triggering OV commit for {ov_session_id}")
                    await session.post(
                        f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/commit",
                        headers=headers
                    )
                    self.pending_tokens[umo] = 0
                    
        except Exception as e:
            logger.error(f"Error syncing to OpenViking: {e}")

    @filter.regex(r"^/new(\s|$)")
    async def handle_new_conversation(self, event: AstrMessageEvent):
        """Intercept /new command via regex to reset OpenViking session mapping without conflicting with built-in command."""
        umo = event.unified_msg_origin
        if umo in self.session_map:
            logger.info(f"Resetting OV session mapping for {umo} due to /new command.")
            del self.session_map[umo]
        # Do not yield anything, allowing the built-in /new command to proceed.
        return None

    @filter.llm_tool(name="memory_recall")
    async def memory_recall(self, event: AstrMessageEvent, query: str):
        """從長期記憶中檢索相關信息。

        Args:
            query(string): 檢索詞或問題。
        """
        try:
            ov_session_id = await self._get_ov_session(event)
            headers = self._get_headers(event)
            async with aiohttp.ClientSession() as session:
                # Search across memories using POST /api/v1/search/search
                async with session.post(
                    f"{self._get_ov_base_url()}/api/v1/search/search",
                    headers=headers,
                    json={
                        "query": query,
                        "session_id": ov_session_id,
                        "limit": 10
                    }
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result_dict = data.get("result", {})
                        
                        # Collect all items from memories, resources, and skills
                        all_items = []
                        if isinstance(result_dict, dict):
                            all_items.extend(result_dict.get("memories", []))
                            all_items.extend(result_dict.get("resources", []))
                            all_items.extend(result_dict.get("skills", []))
                        elif isinstance(result_dict, list):
                            # Fallback if the API returns a flat list in some versions
                            all_items = result_dict
                            
                        if not all_items:
                            return "未找到相關記憶。"
                        
                        ret = "[檢索到的記憶片段]:\n"
                        for item in all_items:
                            if not isinstance(item, dict):
                                continue
                            uri = item.get('uri', 'unknown')
                            abstract = item.get('abstract') or item.get('content', '')
                            # Truncate abstract if too long
                            if len(abstract) > 300:
                                abstract = abstract[:300] + "..."
                            ret += f"- [{uri}]: {abstract}\n"
                        return ret
                    else:
                        return f"記憶檢索失敗: {resp.status} - {await resp.text()}"
        except Exception as e:
            return f"記憶檢索發生錯誤: {str(e)}"

    @filter.llm_tool(name="memory_store")
    async def memory_store(self, event: AstrMessageEvent, fact: str):
        """將特定的重要事實立即存入長期記憶。

        Args:
            fact(string): 需要記住的事實。
        """
        try:
            ov_session_id = await self._get_ov_session(event)
            headers = self._get_headers(event)
            async with aiohttp.ClientSession() as session:
                # Add as a special message and trigger commit
                await session.post(
                    f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/messages",
                    headers=headers,
                    json={"role": "system", "content": f"[IMPORTANT FACT]: {fact}"}
                )
                await session.post(
                    f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/commit",
                    headers=headers
                )
                return "事實已存入長期記憶並建立索引。"
        except Exception as e:
            return f"記憶存儲失敗: {str(e)}"

    @filter.llm_tool(name="archive_expand")
    async def archive_expand(self, event: AstrMessageEvent, archive_id: str):
        """展開特定的歷史歸檔以查看詳細對話記錄。

        Args:
            archive_id(string): 歸檔 ID。
        """
        try:
            ov_session_id = await self._get_ov_session(event)
            headers = self._get_headers(event)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/archives/{archive_id}",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        messages = data.get("result", {}).get("messages", [])
                        if not messages:
                            return "該歸檔內容為空。"
                        
                        ret = f"[歸檔 {archive_id} 的詳細內容]:\n"
                        for msg in messages:
                            ret += f"{msg.get('role', 'unknown')}: {msg.get('content', '')}\n"
                        return ret
                    else:
                        return f"無法展開歸檔: {resp.status}"
        except Exception as e:
            return f"展開歸檔發生錯誤: {str(e)}"

    @filter.llm_tool(name="memory_forget")
    async def memory_forget(self, event: AstrMessageEvent, uri: str):
        """刪除特定的長期記憶。Agent 應先通過 memory_recall 獲取目標記憶的 URI。

        Args:
            uri(string): 記憶的唯一標識符 (URI)。
        """
        try:
            headers = self._get_headers(event)
            async with aiohttp.ClientSession() as session:
                # OpenViking deletes via DELETE /api/v1/fs?uri=...
                async with session.delete(
                    f"{self._get_ov_base_url()}/api/v1/fs?uri={uri}",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        return f"記憶 {uri} 已成功刪除，我將不再記得這件事。"
                    else:
                        return f"刪除記憶失敗: {resp.status} - {await resp.text()}"
        except Exception as e:
            return f"刪除記憶時發生錯誤: {str(e)}"

    async def terminate(self):
        """Plugin shutdown."""
        logger.info("OpenViking Memory Plugin shutting down.")
