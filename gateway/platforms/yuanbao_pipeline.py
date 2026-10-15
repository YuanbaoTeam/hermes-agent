"""Default Yuanbao inbound middleware order.

Kept beside the adapter so adding the xCard intercept does not grow ``yuanbao.py``.
``build`` imports the middleware classes lazily: this module is imported while ``yuanbao``
is still initializing.
"""

from __future__ import annotations


class InboundPipelineBuilder:
    """Assembles the default Yuanbao inbound pipeline (order matters)."""

    @classmethod
    def build(cls):
        from gateway.platforms.yuanbao import (
            AccessGuardMiddleware,
            AutoSetHomeMiddleware,
            BuildSourceMiddleware,
            ChatRoutingMiddleware,
            ClassifyMessageTypeMiddleware,
            DecodeMiddleware,
            DedupMiddleware,
            DispatchMiddleware,
            ExtractContentMiddleware,
            ExtractFieldsMiddleware,
            ForwardedRecordsParseMiddleware,
            GroupAtGuardMiddleware,
            GroupAttributionMiddleware,
            InboundPipeline,
            MediaResolveMiddleware,
            OwnerCommandMiddleware,
            PatchAnchorsMiddleware,
            PlaceholderFilterMiddleware,
            QuoteContextMiddleware,
            RecallGuardMiddleware,
            SkipSelfMiddleware,
        )
        from gateway.platforms.yuanbao_xcard import XCardUpEventMiddleware

        pipeline = InboundPipeline()
        for middleware in (
            DecodeMiddleware, ExtractFieldsMiddleware, RecallGuardMiddleware, DedupMiddleware,
            SkipSelfMiddleware, ChatRoutingMiddleware, AccessGuardMiddleware, XCardUpEventMiddleware,
            ExtractContentMiddleware, PlaceholderFilterMiddleware, OwnerCommandMiddleware,
            BuildSourceMiddleware, GroupAtGuardMiddleware, AutoSetHomeMiddleware,
            GroupAttributionMiddleware, ClassifyMessageTypeMiddleware, QuoteContextMiddleware,
            ForwardedRecordsParseMiddleware, MediaResolveMiddleware, PatchAnchorsMiddleware,
            DispatchMiddleware,
        ):
            pipeline.use(middleware())
        return pipeline
