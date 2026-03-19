from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.middleware import CallNext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams, TextContent

from app.core.log import logger
from app.util.compaction import build_compaction_stage, validate_compaction_stages


class CompactorMiddleware(Middleware):
    def __init__(self, *, cleanup_stages: list[str]):
        normalized_stages = validate_compaction_stages(cleanup_stages)
        if not normalized_stages:
            raise ValueError("At least one cleanup stage must be specified")

        self.cleanup_stages = tuple(normalized_stages)
        self._stage_functions = tuple(build_compaction_stage(stage) for stage in self.cleanup_stages)

    def _compact_text(self, text: str) -> str:
        compacted = text
        for stage_function in self._stage_functions:
            compacted = stage_function(compacted)
        return compacted

    def _compact_structured_value(self, value: object) -> tuple[object, int, int, int, bool]:
        if isinstance(value, str):
            compacted = self._compact_text(value)
            return compacted, 1, len(value), len(compacted), compacted != value

        if isinstance(value, list):
            items: list[object] = []
            string_count = 0
            chars_before = 0
            chars_after = 0
            changed = False

            for item in value:
                (
                    compacted_item,
                    item_count,
                    item_before,
                    item_after,
                    item_changed,
                ) = self._compact_structured_value(item)
                items.append(compacted_item)
                string_count += item_count
                chars_before += item_before
                chars_after += item_after
                changed = changed or item_changed

            return items, string_count, chars_before, chars_after, changed

        if isinstance(value, dict):
            compacted_dict: dict[object, object] = {}
            string_count = 0
            chars_before = 0
            chars_after = 0
            changed = False

            for key, item in value.items():
                (
                    compacted_item,
                    item_count,
                    item_before,
                    item_after,
                    item_changed,
                ) = self._compact_structured_value(item)
                compacted_dict[key] = compacted_item
                string_count += item_count
                chars_before += item_before
                chars_after += item_after
                changed = changed or item_changed

            return compacted_dict, string_count, chars_before, chars_after, changed

        return value, 0, 0, 0, False

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result = await call_next(context)

        updated_content: list[object] = []
        content_string_count = 0
        content_chars_before = 0
        content_chars_after = 0
        content_changed = False

        for block in result.content:
            if not isinstance(block, TextContent):
                updated_content.append(block)
                continue

            compacted_text = self._compact_text(block.text)
            updated_content.append(block.model_copy(update={"text": compacted_text}))
            content_string_count += 1
            content_chars_before += len(block.text)
            content_chars_after += len(compacted_text)
            content_changed = content_changed or compacted_text != block.text

        structured_content = result.structured_content
        structured_string_count = 0
        structured_chars_before = 0
        structured_chars_after = 0
        structured_changed = False

        if structured_content is not None:
            (
                structured_content,
                structured_string_count,
                structured_chars_before,
                structured_chars_after,
                structured_changed,
            ) = self._compact_structured_value(structured_content)

        total_string_count = content_string_count + structured_string_count
        total_chars_before = content_chars_before + structured_chars_before
        total_chars_after = content_chars_after + structured_chars_after

        if total_string_count == 0:
            return result

        if not content_changed and not structured_changed:
            logger.debug(
                f"CompactorMiddleware left tool={context.message.name} unchanged; "
                f"stages={self.cleanup_stages}; "
                f"strings={total_string_count}; "
                f"chars={total_chars_before}"
            )
            return result

        logger.info(
            f"CompactorMiddleware compacted tool={context.message.name}; "
            f"stages={self.cleanup_stages}; "
            f"strings={total_string_count}; "
            f"chars_before={total_chars_before}; "
            f"chars_after={total_chars_after};"
        )
        return result.model_copy(
            update={
                "content": updated_content,
                "structured_content": structured_content,
            }
        )
