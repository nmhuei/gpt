from __future__ import annotations

from collections.abc import Callable
from typing import Any

from playwright.async_api import Page

from gpt.types import ProbeEvent

_JS_INSTRUMENTATION_CODE = r"""
(function() {
    if (window.__bqa_instrumented) return;
    window.__bqa_instrumented = true;

    function emit(source, kind, data) {
        try {
            if (typeof window.__bqa_emit_event === 'function') {
                window.__bqa_emit_event({
                    source: source,
                    kind: kind,
                    timestamp: Date.now(),
                    ...data
                });
            }
        } catch (e) {
            // Ignore emission errors
        }
    }

    // 1. Instrument window.fetch
    const originalFetch = window.fetch;
    window.fetch = async function(...args) {
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || 'unknown';
        const options = args[1] || (typeof args[0] === 'object' ? args[0] : {}) || {};
        const method = (options.method || 'GET').toUpperCase();
        
        let bodySnippet = null;
        if (options.body) {
            if (typeof options.body === 'string') {
                bodySnippet = options.body.slice(0, 100000);
            }
        }

        emit('fetch', 'fetch_start', {
            url: url,
            method: method,
            has_body: !!options.body,
            body_snippet: bodySnippet
        });

        try {
            const response = await originalFetch.apply(this, args);
            const status = response.status;
            const contentType = response.headers.get('content-type') || '';

            emit('fetch', 'fetch_response', {
                url: url,
                method: method,
                status: status,
                content_type: contentType
            });

            // If response is SSE or text stream, optionally clone and observe chunks
            if (contentType.includes('text/event-stream') && response.body && !response.bodyUsed) {
                try {
                    const cloned = response.clone();
                    const reader = cloned.body.getReader();
                    const decoder = new TextDecoder();
                    let totalBytes = 0;

                    (async () => {
                        while (true) {
                            const { done, value } = await reader.read();
                            if (done) {
                                emit('fetch', 'stream_done', { url: url, total_bytes: totalBytes });
                                break;
                            }
                            totalBytes += value.length;
                            const textChunk = decoder.decode(value, { stream: true });
                            emit('fetch', 'stream_chunk', {
                                url: url,
                                chunk_size: value.length,
                                text_snippet: textChunk.slice(0, 10000)
                            });
                        }
                    })().catch(() => {});
                } catch (err) {}
            }

            return response;
        } catch (err) {
            emit('fetch', 'fetch_error', {
                url: url,
                method: method,
                error: err.message
            });
            throw err;
        }
    };

    // 2. Instrument WebSocket
    const OriginalWebSocket = window.WebSocket;
    window.WebSocket = function(url, protocols) {
        const ws = new OriginalWebSocket(url, protocols);
        emit('websocket', 'ws_construct', { url: url });

        ws.addEventListener('open', () => {
            emit('websocket', 'ws_open', { url: url });
        });
        ws.addEventListener('close', (e) => {
            emit('websocket', 'ws_close', { url: url, code: e.code, reason: e.reason });
        });
        ws.addEventListener('error', (e) => {
            emit('websocket', 'ws_error', { url: url });
        });
        ws.addEventListener('message', (e) => {
            let dataStr = typeof e.data === 'string' ? e.data.slice(0, 10000) : '<binary>';
            emit('websocket', 'ws_message_in', { url: url, data: dataStr });
        });

        const origSend = ws.send;
        ws.send = function(data) {
            let dataStr = typeof data === 'string' ? data.slice(0, 10000) : '<binary>';
            emit('websocket', 'ws_message_out', { url: url, data: dataStr });
            return origSend.apply(this, arguments);
        };

        return ws;
    };
    window.WebSocket.prototype = OriginalWebSocket.prototype;

    // 3. Instrument EventSource without changing its public prototype.
    const OriginalEventSource = window.EventSource;
    if (OriginalEventSource) {
        window.EventSource = function(url, config) {
            const source = new OriginalEventSource(url, config);
            emit('eventsource', 'eventsource_construct', { url: String(url) });
            source.addEventListener('open', () => emit('eventsource', 'eventsource_open', { url: String(url) }));
            source.addEventListener('message', (event) => emit('eventsource', 'eventsource_message', {
                url: String(url), data: String(event.data).slice(0, 10000)
            }));
            source.addEventListener('error', () => emit('eventsource', 'eventsource_error', { url: String(url) }));
            return source;
        };
        window.EventSource.prototype = OriginalEventSource.prototype;
    }

    // 4. Observe XHR metadata. Do not read response bodies or alter callbacks.
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__bqa_method = String(method || 'GET').toUpperCase();
        this.__bqa_url = String(url || '');
        return originalOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        emit('xhr', 'xhr_start', {
            url: this.__bqa_url, method: this.__bqa_method,
            body_snippet: typeof body === 'string' ? body.slice(0, 100000) : null
        });
        this.addEventListener('loadend', () => emit('xhr', 'xhr_done', {
            url: this.__bqa_url, method: this.__bqa_method, status: this.status
        }), { once: true });
        return originalSend.apply(this, arguments);
    };

    // 5. Instrument History API
    const origPush = history.pushState;
    history.pushState = function(state, title, url) {
        emit('history', 'push_state', { url: url ? url.toString() : null });
        return origPush.apply(this, arguments);
    };

    const origReplace = history.replaceState;
    history.replaceState = function(state, title, url) {
        emit('history', 'replace_state', { url: url ? url.toString() : null });
        return origReplace.apply(this, arguments);
    };
})();
"""


class JSProbeManager:
    """Installs client-side JavaScript probes into the page context."""

    def __init__(self, on_event_callback: Callable[[ProbeEvent], None] | None = None):
        self.on_event_callback = on_event_callback
        self.events: list[ProbeEvent] = []
        self._seq = 0
        self._active_experiment_id: str | None = None

    def set_experiment_id(self, experiment_id: str | None) -> None:
        self._active_experiment_id = experiment_id

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def install(self, page: Page) -> None:
        async def _binding_handler(_source, payload: dict[str, Any]):
            source = payload.pop("source", "fetch")
            kind = payload.pop("kind", "unknown")
            url = payload.pop("url", None)
            method = payload.pop("method", None)
            status = payload.pop("status", None)

            event = ProbeEvent.create(
                source=source,
                kind=kind,
                sequence=self._next_seq(),
                experiment_id=self._active_experiment_id,
                url=url,
                method=method,
                status=status,
                metadata=payload,
            )
            self.events.append(event)
            if self.on_event_callback:
                try:
                    self.on_event_callback(event)
                except Exception:
                    pass

        try:
            await page.expose_binding("__bqa_emit_event", _binding_handler)
        except Exception:
            pass  # Already exposed

        await page.add_init_script(_JS_INSTRUMENTATION_CODE)
        try:
            await page.evaluate(_JS_INSTRUMENTATION_CODE)
        except Exception:
            pass
