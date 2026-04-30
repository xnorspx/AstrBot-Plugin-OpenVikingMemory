import aiohttp
import asyncio
import hashlib
import os
import time
import uuid
import yaml
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

# Load metadata from metadata.yaml as a single source of truth
metadata_path = os.path.join(os.path.dirname(__file__), "metadata.yaml")
with open(metadata_path, encoding="utf-8") as f:
    metadata = yaml.safe_load(f)

# Strip 'v' prefix from version if present
plugin_version = metadata.get("version", "1.0.0").lstrip("v")
plugin_name = metadata.get("name", "openviking_memory")
plugin_author = metadata.get("author", "Sunny")
plugin_desc = metadata.get("desc", "OpenViking Long-term Memory Plugin")


@register(plugin_name, plugin_author, plugin_desc, plugin_version)
class OpenVikingMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config else {}
        self.session_map: Dict[str, str] = {}
        self.pending_tokens: Dict[str, int] = {}
        self.session: Optional[aiohttp.ClientSession] = None

    async def initialize(self):
        """Initialize plugin."""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"Content-Type": "application/json"}
        )
        logger.info(
            f"OpenViking Memory Plugin v{plugin_version} initialized. Target: {self._get_ov_base_url()}"
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
            try:
                async with self.session.post(
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
                        # Do not cache invalid MD5 hash to prevent cache poisoning.
                        # Return a temporary one but don't save it to self.session_map.
                        return hashlib.md5(mapping_key.encode()).hexdigest()
            except Exception as e:
                logger.error(f"Error creating OV session: {e}")
                return hashlib.md5(mapping_key.encode()).hexdigest()
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
            # Store ambient context for synchronization in after_llm_response (Scenario A)
            if request.contexts:
                event.set_extra("ov_ambient_context", request.contexts)

            # Resolve the active persona ID
            conv_persona_id = (
                request.conversation.persona_id if request.conversation else None
            )
            persona_id = await self._resolve_persona_id(event, conv_persona_id)

            ov_session_id = await self._get_ov_session(event, persona_id)
            headers = self._get_headers(event, persona_id)

            async with self.session.get(
                f"{self._get_ov_base_url()}/api/v1/sessions/{ov_session_id}/context?token_budget=800",
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    ctx_data = await resp.json()
                    ov_ctx = ctx_data.get("result", {})
                    
                    # Optimize: Silent Injection. Only inject when there is actual content.
                    content_parts = []
                    
                    # 1. Latest Archive Overview (Summary of the most recent finished segment)
                    latest_ov = ov_ctx.get("latest_archive_overview")
                    if latest_ov:
                        content_parts.append(f"[最近會話總結]:\n{latest_ov}")
                    
                    # 2. Historical Archive Abstracts (Chronological context tracking)
                    pre_abstracts = ov_ctx.get("pre_archive_abstracts", [])
                    if pre_abstracts:
                        abstract_lines = ["[歷史會話脈絡]:"]
                        for arch in pre_abstracts:
                            arch_id = arch.get("archive_id", "unknown")
                            abstract_lines.append(f"- [ID: {arch_id}] {arch.get('abstract', '')}")
                        content_parts.append("\n".join(abstract_lines))
                    
                    if content_parts:
                        memory_block = "\n<relevant-memories>\n"
                        memory_block += "\n\n".join(content_parts)
                        memory_block += "\n\n(如果你需要獲取更詳細的信息或回想更多細節，可以調用 memory_recall 工具。)\n"
                        memory_block += "</relevant-memories>\n"
                        
                        # Fix: Safe injection to prevent TypeError if prompt/system_prompt is None
                        if request.system_prompt is not None:
                            request.system_prompt = memory_block + request.system_prompt
                        elif request.prompt is not None:
                            request.prompt = memory_block + request.prompt
                        else:
                            request.system_prompt = memory_block
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

            base_url = self._get_ov_base_url()

            # Scenario A: Sync Ambient Context
            ambient_context = event.get_extra("ov_ambient_context")
            if ambient_context and isinstance(ambient_context, list):
                # Find messages after the last assistant response to avoid duplication
                new_ambient = []
                for msg in reversed(ambient_context):
                    if msg.get("role") == "assistant":
                        break
                    new_ambient.insert(0, msg)
                
                if new_ambient:
                    ambient_lines = []
                    for msg in new_ambient:
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        # Handle multimodal content
                        if isinstance(content, list):
                            text_parts = []
                            for p in content:
                                if p.get("type") == "text":
                                    text_parts.append(p.get("text", ""))
                                else:
                                    text_parts.append(f"[{p.get('type', 'media')}]")
                            content = " ".join(text_parts)
                        
                        if content and str(content).strip():
                            ambient_lines.append(f"{role}: {str(content).strip()}")
                    
                    if ambient_lines:
                        ambient_text = "[群聊背景上下文]:\n" + "\n".join(ambient_lines)
                        await self.session.post(
                            f"{base_url}/api/v1/sessions/{ov_session_id}/messages",
                            headers=headers,
                            json={"role": "system", "content": ambient_text},
                        )

            # Sync current interaction
            await self.session.post(
                f"{base_url}/api/v1/sessions/{ov_session_id}/messages",
                headers=headers,
                json={"role": "user", "content": user_text},
            )
            await self.session.post(
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
                await self.session.post(
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
            async with self.session.post(
                f"{self._get_ov_base_url()}/api/v1/search/find",
                headers=headers,
                json={
                    "query": query,
                    "target_uri": "viking://",
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
                    # Fetch full content for level-2 memories in parallel to ensure details (like UID) are retrieved
                    async def fetch_full_content(item):
                        uri = item.get("uri", "unknown")
                        level = item.get("level", 2)
                        if level == 2:
                            try:
                                async with self.session.get(
                                    f"{self._get_ov_base_url()}/api/v1/content/read?uri={uri}",
                                    headers=headers,
                                ) as c_resp:
                                    if c_resp.status == 200:
                                        c_data = await c_resp.json()
                                        full_content = c_data.get("result", "")
                                        if full_content:
                                            return f"- [{uri}]: {full_content[:1000]}"
                            except Exception as e:
                                logger.error(f"Error reading full content for {uri}: {e}")
                        
                        abstract = item.get("abstract") or item.get("content") or "無內容"
                        return f"- [{uri}]: {abstract[:300]}"

                    tasks = [fetch_full_content(item) for item in all_items]
                    results = await asyncio.gather(*tasks)
                    
                    ret = "[檢索到的記憶片段]:\n"
                    ret += "\n".join(results)
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
            base_url = self._get_ov_base_url()
            await self.session.post(
                f"{base_url}/api/v1/sessions/{ov_session_id}/messages",
                headers=headers,
                json={"role": "system", "content": f"[IMPORTANT FACT]: {fact}"},
            )
            await self.session.post(
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
            async with self.session.get(
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
            async with self.session.delete(
                f"{self._get_ov_base_url()}/api/v1/fs?uri={uri}", headers=headers
            ) as resp:
                if resp.status == 200:
                    return f"記憶 {uri} 已刪除。"
                return f"刪除失敗: {resp.status}"
        except Exception as e:
            return f"刪除錯誤: {str(e)}"

    async def terminate(self):
        if self.session:
            await self.session.close()
        logger.info("OpenViking Memory Plugin shutting down.")
