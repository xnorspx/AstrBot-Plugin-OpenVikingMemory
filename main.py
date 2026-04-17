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


@register("openviking_memory", "Sunny", "OpenViking Long-term Memory Plugin", "1.0.6")
class OpenVikingMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config else {}
        self.session_map: Dict[str, str] = {}
        self.pending_tokens: Dict[str, int] = {}

    async def initialize(self):
        """Initialize plugin."""
        logger.info(
            f"OpenViking Memory Plugin v1.0.6 initialized. Target: {self._get_ov_base_url()}"
        )

    def _get_ov_base_url(self) -> str:
        url = self.config.get("ov_base_url", "http://127.0.0.1:1933")
        return url.rstrip("/")

    def _get_api_key(self) -> str:
        return self.config.get("api_key", "")

    def _get_commit_threshold(self) -> int:
        return self.config.get("commit_threshold", 2000)

    async def _resolve_persona_id(
        self, event: AstrMessageEvent, conversation_persona_id: Optional[str] = None
    ) -> str:
        """Resolve the active Persona ID using AstrBot's resolution logic."""
        try:
            # If conversation_persona_id is not provided, try to get it from DB
            if not conversation_persona_id:
                conv_mgr = self.context.conversation_manager
                cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
                if cid:
                    conv = await conv_mgr.get_conversation(
                        event.unified_msg_origin, cid
                    )
                    if conv:
                        conversation_persona_id = conv.persona_id

            # Use AstrBot's PersonaManager to resolve the final persona
            # This handles session rules, conversation settings, and global defaults
            persona_mgr = self.context.persona_manager
            provider_settings = self.context.astrbot_config_mgr.get_conf(
                event.unified_msg_origin
            ).get("provider_settings", {})

            persona_id, _, _, _ = await persona_mgr.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=conversation_persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=provider_settings,
            )

            if persona_id:
                return str(persona_id)
        except Exception as e:
            logger.error(f"Error resolving persona_id: {e}")

        return "default"

    def _get_headers(self, event: AstrMessageEvent, persona_id: str) -> Dict[str, str]:
        """Construct routing headers with persona-aware Agent IDs for strict isolation."""
        platform = event.get_platform_name()
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        # Account ID per platform
        account_id = f"astrbot_{platform}"

        # User ID is platform-scoped user
        ov_user_id = f"{platform}_{user_id}"

        # Agent ID is now Bot-specific using persona_id to prevent multi-bot collision
        if group_id:
            agent_id = f"agent_{platform}_{persona_id}_group_{group_id}"
        else:
            agent_id = f"agent_{platform}_{persona_id}_user_{user_id}"

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

    async def _get_ov_session(self, event: AstrMessageEvent, persona_id: str) -> str:
        umo = event.unified_msg_origin
        mapping_key = f"{umo}:{persona_id}"
        if mapping_key not in self.session_map:
            headers = self._get_headers(event, persona_id)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._get_ov_base_url()}/api/v1/sessions",
                    headers=headers,
                    json={},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.session_map[mapping_key] = data["result"]["session_id"]
                    else:
                        logger.error(
                            f"Failed to create OV session: {await resp.text()}"
                        )
                        self.session_map[mapping_key] = hashlib.md5(
                            mapping_key.encode()
                        ).hexdigest()
        return self.session_map[mapping_key]

    def _degrade_message(self, chain: List[BaseMessageComponent]) -> str:
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
    async def before_llm_request(
        self, event: AstrMessageEvent, request: ProviderRequest
    ):
        if event.get_platform_name() == "dashboard":
            return
        try:
            # Resolve the active persona ID
            conv_persona_id = (
                request.conversation.persona_id if request.conversation else None
            )
            persona_id = await self._resolve_persona_id(event, conv_persona_id)

            ov_session_id = await self._get_ov_session(event, persona_id)
            headers = self._get_headers(event, persona_id)

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/context?token_budget=800",
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        ctx_data = await resp.json()
                        ov_ctx = ctx_data.get("result", {})
                        memory_block = "\n<relevant-memories>\n"

                        mems = ov_ctx.get("memories", [])
                        if mems:
                            memory_block += "[長期記憶與經驗教訓]:\n"
                            for m in mems:
                                memory_block += f"- {m.get('abstract', '')}\n"
                            memory_block += "\n"

                        if "archives" in ov_ctx and ov_ctx["archives"]:
                            memory_block += "[近期會話歸檔摘要]:\n"
                            for arch in ov_ctx["archives"]:
                                memory_block += f"- {arch.get('overview', '')}\n"

                        memory_block += "\n(如果你需要獲取更詳細的信息或回想更多細節，可以調用 memory_recall 工具。)\n"
                        memory_block += "</relevant-memories>\n"
                        if request.system_prompt:
                            request.system_prompt = memory_block + request.system_prompt
                        else:
                            request.prompt = memory_block + request.prompt
        except Exception as e:
            logger.error(f"Error in OpenViking Shallow Intuition: {e}")

    @filter.on_llm_response()
    async def after_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        if event.get_platform_name() == "dashboard":
            return
        try:
            persona_id = await self._resolve_persona_id(event)
            ov_session_id = await self._get_ov_session(event, persona_id)
            headers = self._get_headers(event, persona_id)
            user_text = self._degrade_message(event.get_messages())
            assistant_text = (
                response.completion_text
                if response.completion_text
                else "[Tool Call/Empty Response]"
            )

            async with aiohttp.ClientSession() as session:
                base_url = self._get_ov_base_url()
                await session.post(
                    f"{base_url}/api/v1/sessions/{ov_session_id}/messages",
                    headers=headers,
                    json={"role": "user", "content": user_text},
                )
                await session.post(
                    f"{base_url}/api/v1/sessions/{ov_session_id}/messages",
                    headers=headers,
                    json={"role": "assistant", "content": assistant_text},
                )

                mapping_key = f"{event.unified_msg_origin}:{persona_id}"
                self.pending_tokens[mapping_key] = (
                    self.pending_tokens.get(mapping_key, 0)
                    + len(user_text)
                    + len(assistant_text)
                )
                if self.pending_tokens[mapping_key] >= self._get_commit_threshold():
                    await session.post(
                        f"{base_url}/api/v1/sessions/{ov_session_id}/commit",
                        headers=headers,
                    )
                    self.pending_tokens[mapping_key] = 0
        except Exception as e:
            logger.error(f"Error syncing to OpenViking: {e}")

    @filter.regex(r"^/new(\s|$)")
    async def handle_new_conversation(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        persona_id = await self._resolve_persona_id(event)
        mapping_key = f"{umo}:{persona_id}"
        if mapping_key in self.session_map:
            del self.session_map[mapping_key]
        return None

    @filter.llm_tool(name="memory_recall")
    async def memory_recall(self, event: AstrMessageEvent, query: str):
        """從長期記憶中檢索相關信息。

        Args:
            query(string): 查詢關鍵詞或問題
        """
        try:
            persona_id = await self._resolve_persona_id(event)
            ov_session_id = await self._get_ov_session(event, persona_id)
            headers = self._get_headers(event, persona_id)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._get_ov_base_url()}/api/v1/search/find",
                    headers=headers,
                    json={
                        "query": query,
                        "target_uri": "viking://user/",
                        "limit": 5,
                        "score_threshold": 0.05,
                    },
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result_dict = data.get("result", {})
                        all_items = []
                        if isinstance(result_dict, dict):
                            all_items.extend(result_dict.get("memories", []))
                            all_items.extend(result_dict.get("resources", []))
                            all_items.extend(result_dict.get("skills", []))
                        if not all_items:
                            return "未找到相關記憶。"
                        ret = "[檢索到的記憶片段]:\n"
                        for item in all_items:
                            uri, abstract = (
                                item.get("uri", "unknown"),
                                item.get("abstract") or item.get("content", ""),
                            )
                            ret += f"- [{uri}]: {abstract[:300]}\n"
                        return ret
                    return f"檢索失敗: {resp.status}"
        except Exception as e:
            return f"檢索錯誤: {str(e)}"

    @filter.llm_tool(name="memory_store")
    async def memory_store(self, event: AstrMessageEvent, fact: str):
        """立即存入事實。

        Args:
            fact(string): 要存入的事實內容
        """
        try:
            persona_id = await self._resolve_persona_id(event)
            ov_session_id = await self._get_ov_session(event, persona_id)
            headers = self._get_headers(event, persona_id)
            async with aiohttp.ClientSession() as session:
                base_url = self._get_ov_base_url()
                await session.post(
                    f"{base_url}/api/v1/sessions/{ov_session_id}/messages",
                    headers=headers,
                    json={"role": "system", "content": f"[IMPORTANT FACT]: {fact}"},
                )
                await session.post(
                    f"{base_url}/api/v1/sessions/{ov_session_id}/commit",
                    headers=headers,
                )
                return "事實已存入並建立索引。"
        except Exception as e:
            return f"存儲失敗: {str(e)}"

    @filter.llm_tool(name="archive_expand")
    async def archive_expand(self, event: AstrMessageEvent, archive_id: str):
        """展開歸檔內容。

        Args:
            archive_id(string): 歸檔 ID
        """
        try:
            persona_id = await self._resolve_persona_id(event)
            ov_session_id = await self._get_ov_session(event, persona_id)
            headers = self._get_headers(event, persona_id)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/archives/{archive_id}",
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        messages = data.get("result", {}).get("messages", [])
                        if not messages:
                            return "歸檔為空。"
                        ret = f"[歸檔 {archive_id} 內容]:\n"
                        for msg in messages:
                            ret += f"{msg.get('role', 'unknown')}: {msg.get('content', '')}\n"
                        return ret
                    return f"展開失敗: {resp.status}"
        except Exception as e:
            return f"展開錯誤: {str(e)}"

    @filter.llm_tool(name="memory_forget")
    async def memory_forget(self, event: AstrMessageEvent, uri: str):
        """遺忘記憶。

        Args:
            uri(string): 要刪除的記憶 URI
        """
        try:
            persona_id = await self._resolve_persona_id(event)
            headers = self._get_headers(event, persona_id)
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{self._get_ov_base_url()}/api/v1/fs?uri={uri}", headers=headers
                ) as resp:
                    if resp.status == 200:
                        return f"記憶 {uri} 已刪除。"
                    return f"刪除失敗: {resp.status}"
        except Exception as e:
            return f"刪除錯誤: {str(e)}"

    async def terminate(self):
        logger.info("OpenViking Memory Plugin shutting down.")
