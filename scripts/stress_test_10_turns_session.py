#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import time
import httpx
from pathlib import Path

BASE_URL = "http://127.0.0.1:18000"

PROMPTS = [
    (1, "Kiểm tra phiên bản trong pyproject.toml và tóm tắt ngắn gọn các dependency chính."),
    (2, "Đọc file gpt/types.py và giải thích vai trò của các dataclass Turn, TurnResult, SendRequest."),
    (3, "Giải thích kiến trúc hoạt động của CompletionRuntime trong gpt/gateway/runtime.py."),
    (4, "Trình bày cơ chế bảo mật và lọc dữ liệu nhạy cảm của default_redactor trong gpt/reverse/redact.py."),
    (5, "Đọc file gpt/state.py và liệt kê các trạng thái của SessionState cùng các ngoại lệ WebChatError."),
    (6, "Phân tích cách ToolTranspiler trong gpt/transpiler/ chuyển đổi tool calls giữa Claude và ChatGPT Web."),
    (7, "Trình bày cơ chế retry và giải quyết xung đột trong ConversationStore (gpt/conversations.py)."),
    (8, "Viết một hàm Python mẫu vào /tmp/benchmark_math.py để tính dãy Fibonacci và kiểm tra số nguyên tố."),
    (9, "Giải thích quy trình 6 bước trong pipeline phân tích PCAP tại docs/superpowers/plans/2026-08-22-pcap-analysis-automation-pipeline.md."),
    (10, "Tổng kết lại toàn bộ các chủ đề đã thảo luận từ turn 1 đến turn 9 trong session này và đánh giá độ ổn định."),
]

async def run_stress_test():
    print("=" * 70)
    print("🚀 BẮT ĐẦU TEST SỨC CHỊU ĐỰNG: 10 PROMPTS LIÊN TỤC TRONG 1 SESSION")
    print(f"🔗 Gateway Target: {BASE_URL}")
    print("=" * 70)
    
    # Verify Gateway health
    async with httpx.AsyncClient(timeout=10.0) as client:
        health_resp = await client.get(f"{BASE_URL}/health")
        print(f"[*] Gateway Health Check: {health_resp.status_code} - {health_resp.json()}")

    session_id = f"stress-session-{int(time.time())}"
    results = []
    messages = []

    async with httpx.AsyncClient(timeout=180.0) as client:
        for turn_idx, prompt in PROMPTS:
            print(f"\n" + "-" * 70)
            print(f"▶️ [Turn {turn_idx}/10] Gửi prompt: {prompt}")
            start_t = time.monotonic()
            
            messages.append({"role": "user", "content": prompt})
            
            payload = {
                "model": "claude-3-5-sonnet",
                "messages": messages,
                "max_tokens": 4096,
                "stream": True,
            }
            
            headers = {
                "x-session-id": session_id,
                "content-type": "application/json",
                "authorization": "Bearer sk-webgpt-local",
            }
            
            collected_text = []
            status_code = None
            error_msg = None
            
            try:
                async with client.stream(
                    "POST",
                    f"{BASE_URL}/v1/messages",
                    json=payload,
                    headers=headers,
                ) as response:
                    status_code = response.status_code
                    if status_code != 200:
                        err_body = await response.aread()
                        error_msg = f"HTTP {status_code}: {err_body.decode('utf-8', errors='replace')}"
                    else:
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            raw_data = line[6:].strip()
                            if raw_data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(raw_data)
                                # Handle anthropic SSE format
                                chunk_type = chunk.get("type")
                                if chunk_type == "content_block_delta":
                                    delta = chunk.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        collected_text.append(delta.get("text", ""))
                                elif chunk_type == "content_block_start":
                                    pass
                                # Handle openai SSE format fallback
                                elif "choices" in chunk:
                                    delta = chunk["choices"][0].get("delta", {})
                                    if "content" in delta and delta["content"]:
                                        collected_text.append(delta["content"])
                            except Exception:
                                pass
            except Exception as exc:
                error_msg = f"Exception: {type(exc).__name__} - {exc}"
            
            duration = time.monotonic() - start_t
            full_response = "".join(collected_text).strip()
            
            # Save assistant reply to session history for context retention
            if full_response:
                messages.append({"role": "assistant", "content": full_response})
            
            success = status_code == 200 and len(full_response) > 0 and error_msg is None
            
            turn_stat = {
                "turn": turn_idx,
                "prompt": prompt,
                "status_code": status_code,
                "duration_seconds": round(duration, 2),
                "response_chars": len(full_response),
                "success": success,
                "error": error_msg,
                "snippet": full_response[:180] + ("..." if len(full_response) > 180 else ""),
            }
            results.append(turn_stat)
            
            icon = "✅" if success else "❌"
            print(f"{icon} [Turn {turn_idx}] Xong trong {duration:.2f}s | {len(full_response)} chars | Status: {status_code}")
            if error_msg:
                print(f"   ⚠️ Lỗi: {error_msg}")
            else:
                print(f"   📝 Nội dung nhận được ({len(full_response)} ký tự):\n   \"{turn_stat['snippet']}\"")

    # Generate Summary Report
    print("\n" + "=" * 70)
    print("📊 BẢNG TỔNG KẾT TEST 10 PROMPTS LIÊN TỤC TRONG 1 SESSION:")
    print("=" * 70)
    passed_count = sum(1 for r in results if r["success"])
    total_time = sum(r["duration_seconds"] for r in results)
    avg_time = total_time / len(results) if results else 0
    total_chars = sum(r["response_chars"] for r in results)
    
    print(f"• Tỉ lệ thành công: {passed_count}/{len(results)} turns ({(passed_count/len(results))*100:.1f}%)")
    print(f"• Tổng thời gian: {total_time:.2f}s (Trung bình {avg_time:.2f}s / turn)")
    print(f"• Tổng lượng văn bản sinh ra: {total_chars} ký tự")
    
    # Save JSON report
    report_file = Path("/home/light/GitHub/gpt/scratch/10_turn_stress_test_report.json")
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps({"summary": {"passed": passed_count, "total": len(results), "total_time_s": total_time, "total_chars": total_chars}, "turns": results}, indent=2, ensure_ascii=False))
    print(f"\n💾 Báo cáo chi tiết đã lưu tại: {report_file}")

if __name__ == "__main__":
    asyncio.run(run_stress_test())
