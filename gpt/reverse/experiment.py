from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from playwright.async_api import Page

from gpt.reverse.artifacts import ArtifactManager
from gpt.reverse.cdp_recorder import CDPRecorder
from gpt.reverse.dom_probe import DOMProbe
from gpt.reverse.js_probe import JSProbeManager
from gpt.reverse.recorder import NetworkRecorder
from gpt.types import Experiment


class ExperimentRunner:
    """Coordinates and correlates single-variable reverse-engineering experiments."""

    def __init__(
        self,
        page: Page,
        artifact_manager: ArtifactManager | None = None,
    ):
        self.page = page
        self.artifact_manager = artifact_manager or ArtifactManager()
        self.net_recorder = NetworkRecorder(page)
        self.cdp_recorder = CDPRecorder(page)
        self.js_probe = JSProbeManager()
        self.dom_probe = DOMProbe(page)

    async def initialize(self) -> None:
        self.net_recorder.attach()
        await self.cdp_recorder.attach()
        await self.js_probe.install(self.page)

    @asynccontextmanager
    async def experiment(
        self,
        exp_id: str,
        variable: str = "",
        description: str = "",
        capture_dom_snapshots: bool = True,
    ) -> AsyncIterator[Experiment]:
        marker = f"BQA_{exp_id}_{uuid.uuid4().hex[:8]}"
        exp = Experiment(
            id=exp_id,
            variable=variable,
            marker=marker,
            started_ns=time.monotonic_ns(),
            description=description,
        )

        run_dir = self.artifact_manager.create_run_dir(exp_id)
        self.net_recorder.set_experiment_id(exp_id)
        self.cdp_recorder.set_experiment_id(exp_id)
        self.js_probe.set_experiment_id(exp_id)

        # Before snapshots
        if capture_dom_snapshots:
            try:
                dom_before = await self.dom_probe.get_dom_html()
                self.artifact_manager.save_text(run_dir, "dom-before.html", dom_before)
                a11y_before = await self.dom_probe.get_accessibility_tree()
                self.artifact_manager.save_json(run_dir, "accessibility-before.json", a11y_before)
                screenshot = await self.page.screenshot()
                self.artifact_manager.save_bytes(run_dir, "screenshot-before.png", screenshot)
            except Exception:
                pass

        try:
            yield exp
        finally:
            exp.ended_ns = time.monotonic_ns()
            self.net_recorder.set_experiment_id(None)
            self.cdp_recorder.set_experiment_id(None)
            self.js_probe.set_experiment_id(None)

            await self.net_recorder.flush()

            # After snapshots
            if capture_dom_snapshots:
                try:
                    dom_after = await self.dom_probe.get_dom_html()
                    self.artifact_manager.save_text(run_dir, "dom-after.html", dom_after)
                    a11y_after = await self.dom_probe.get_accessibility_tree()
                    self.artifact_manager.save_json(run_dir, "accessibility-after.json", a11y_after)
                    screenshot = await self.page.screenshot()
                    self.artifact_manager.save_bytes(run_dir, "screenshot-after.png", screenshot)
                except Exception:
                    pass

            # Gather all events for this experiment
            exp_events = [
                asdict(e)
                for e in (
                    self.net_recorder.events
                    + self.cdp_recorder.events
                    + self.js_probe.events
                )
                if e.experiment_id == exp_id
            ]
            exp_events.sort(key=lambda event: event["monotonic_ns"])

            # Save artifacts
            self.artifact_manager.save_json(run_dir, "meta.json", asdict(exp))
            self.artifact_manager.save_ndjson(run_dir, "events.ndjson", exp_events)

            summary: dict[str, Any] = {
                "experiment_id": exp.id,
                "variable": exp.variable,
                "marker": exp.marker,
                "duration_ms": (exp.ended_ns - exp.started_ns) / 1_000_000,
                "total_events": len(exp_events),
                "events_by_source": {},
            }
            for ev in exp_events:
                src = ev.get("source", "unknown")
                summary["events_by_source"][src] = summary["events_by_source"].get(src, 0) + 1

            self.artifact_manager.save_json(run_dir, "summary.json", summary)
