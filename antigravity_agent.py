#!/usr/bin/env python3
import os
import sys
import json
import subprocess
import urllib.request
import argparse
import traceback
import time

def parse_pages(pages_text):
    pages = []
    # Lines look like:
    # 1: about:blank [selected]
    # 2: https://example.com
    for line in pages_text.splitlines():
        line = line.strip()
        if not line or line.startswith("##"):
            continue
        parts = line.split(":", 1)
        if len(parts) == 2:
            try:
                pid = int(parts[0].strip())
                rest = parts[1].strip()
                is_selected = "[selected]" in rest
                url = rest.replace("[selected]", "").strip()
                pages.append({"id": pid, "url": url, "selected": is_selected})
            except ValueError:
                pass
    return pages

class McpClient:
    def __init__(self):
        # Redirect stderr to a log file to avoid polluting stdin/stdout parsing
        os.makedirs("reports", exist_ok=True)
        self.stderr_log = open("reports/mcp_server_stderr.log", "w", encoding="utf-8")
        
        # Default values to start the MCP server
        cmd = "npx"
        args = ["-y", "chrome-devtools-mcp"]
        env = os.environ.copy()
        
        # Try to load local configuration from .agents/mcp_config.json
        config_path = os.path.join(".agents", "mcp_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                    server_config = config_data.get("mcpServers", {}).get("chrome-devtools", {})
                    if server_config:
                        cmd = server_config.get("command", cmd)
                        args = list(server_config.get("args", args))
                        # Merge environment variables
                        custom_env = server_config.get("env", {})
                        for k, v in custom_env.items():
                            env[str(k)] = str(v)
                        print(f"Loaded MCP configuration from {config_path}", flush=True)
            except Exception as e:
                print(f"Warning: Could not read {config_path}: {e}", file=sys.stderr)

        user_data_path = os.path.abspath(".agents/chrome-profile")
        # Ensure Chrome profile is configured in isolation if not specified in arguments
        has_user_data_dir = any(arg.startswith("--user-data-dir") for arg in args)
        if not has_user_data_dir:
            args.append(f"--user-data-dir={user_data_path}")

        print(f"Starting MCP server: {cmd} {' '.join(args)}", flush=True)
        self.process = subprocess.Popen(
            [cmd] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_log,
            env=env,
            text=True,
            bufsize=1
        )
        self._id = 0

    def send_request(self, method, params):
        self._id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params
        }
        req_str = json.dumps(req)
        self.process.stdin.write(req_str + "\n")
        self.process.stdin.flush()
        return self.read_response(self._id)

    def send_notification(self, method, params=None):
        req = {
            "jsonrpc": "2.0",
            "method": method
        }
        if params:
            req["params"] = params
        req_str = json.dumps(req)
        self.process.stdin.write(req_str + "\n")
        self.process.stdin.flush()

    def read_response(self, req_id):
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed connection unexpectedly.")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Skip non-JSON lines if any
                continue
            
            # Check if it is the response to our request
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"MCP Error: {msg['error']}")
                return msg.get("result")

    def initialize(self):
        res = self.send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "antigravity-qa-runner",
                "version": "1.0.0"
            }
        })
        self.send_notification("notifications/initialized")
        print("MCP server initialized successfully.", flush=True)
        return res

    def call_tool(self, name, arguments):
        return self.send_request("tools/call", {
            "name": name,
            "arguments": arguments
        })

    def call_tool_text(self, name, arguments):
        res = self.call_tool(name, arguments)
        if not res or "content" not in res:
            return ""
        text_parts = []
        for item in res["content"]:
            if item.get("type") == "text":
                text_parts.append(item.get("text", ""))
        return "\n".join(text_parts)

    def ensure_page_selected(self):
        pages_text = self.call_tool_text("list_pages", {})
        pages = parse_pages(pages_text)
        if not pages:
            print("No open tabs found. Opening a new one...", flush=True)
            self.call_tool("new_page", {"url": "about:blank"})
            time.sleep(1) # Wait for page to open
        else:
            has_selected = any(p["selected"] for p in pages)
            if not has_selected:
                print(f"Selecting default tab: ID {pages[0]['id']}", flush=True)
                self.call_tool("select_page", {"pageId": pages[0]["id"]})

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.stderr_log.close()

def parse_spec(spec_path):
    objective = ""
    actions = []
    if not os.path.exists(spec_path):
        raise FileNotFoundError(f"Specification file not found: {spec_path}")
        
    with open(spec_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Normalize prefixes for both Spanish and English
            if line.startswith("- **Objetivo:**") or line.startswith("- **Objetivo**"):
                objective = line.replace("- **Objetivo:**", "").replace("- **Objetivo**", "").strip()
            elif line.startswith("- **Objective:**") or line.startswith("- **Objective**"):
                objective = line.replace("- **Objective:**", "").replace("- **Objective**", "").strip()
            elif line.startswith("- **Acción:**") or line.startswith("- **Acción**"):
                action = line.replace("- **Acción:**", "").replace("- **Acción**", "").strip()
                actions.append(action)
            elif line.startswith("- **Action:**") or line.startswith("- **Action**"):
                action = line.replace("- **Action:**", "").replace("- **Action**", "").strip()
                actions.append(action)
            elif line.startswith("- ") and any(k in line for k in ["Objetivo:", "Objetivo", "Objective:", "Objective"]):
                parts = line.split(":", 1)
                objective = parts[1].strip()
            elif line.startswith("- ") and any(k in line for k in ["Acción:", "Acción", "Action:", "Action"]):
                parts = line.split(":", 1)
                actions.append(parts[1].strip())
    return objective, actions

def clean_json_text(text):
    text = text.strip()
    
    start_idx = text.find("{")
    if start_idx == -1:
        return text
        
    brace_count = 0
    in_string = False
    escape = False
    
    for i in range(start_idx, len(text)):
        char = text[i]
        
        if escape:
            escape = False
            continue
            
        if char == '\\':
            escape = True
            continue
            
        if char == '"':
            in_string = not in_string
            continue
            
        if not in_string:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    return text[start_idx:i+1]
                    
    # Fallback if braces were not closed in a balanced way
    end_idx = text.rfind("}")
    if end_idx != -1 and end_idx > start_idx:
        return text[start_idx:end_idx+1]
        
    return text.strip()

def call_gemini(api_key, system_instruction, contents):
    model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            text = res_data["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        try:
            err_json = json.loads(err_msg)
            message = err_json.get("error", {}).get("message", "Unknown network error")
        except Exception:
            message = err_msg
        raise RuntimeError(f"Gemini API Error ({e.code}): {message}") from e
    except Exception as e:
        raise RuntimeError(f"Error querying Gemini: {e}") from e

    try:
        cleaned = clean_json_text(text)
        return json.loads(cleaned)
    except Exception as e:
        print(f"Error parsing Gemini response. Raw text received:\n{text}", file=sys.stderr)
        raise RuntimeError(f"Error parsing Gemini JSON response: {e}. Raw text: {text[:200]}") from e

def run_qa_agent(spec_path, base_url, api_key):
    # Parse specification
    objective, actions = parse_spec(spec_path)
    print("\n" + "="*50)
    print(f"STARTING QA AGENT")
    print(f"Specification: {spec_path}")
    print(f"Objective: {objective}")
    print(f"Base URL: {base_url}")
    print("="*50 + "\n")

    # Start MCP client
    client = McpClient()
    history = []
    status = "running"
    final_message = ""
    
    # Create a clean name for reports
    flow_name = os.path.splitext(os.path.basename(spec_path))[0]
    
    # Clean the URL for the report directory name (report_[flow]_[audited_url]_[date]_[time])
    import re
    from datetime import datetime
    url_clean = base_url.replace("http://", "").replace("https://", "")
    url_clean = re.sub(r'[^a-zA-Z0-9]', '_', url_clean)
    url_clean = re.sub(r'_+', '_', url_clean).strip('_')
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_basename = f"report_{flow_name}_{url_clean}_{timestamp}"
    report_dir = os.path.join("reports", report_basename)
    
    report_data = {}
    os.makedirs(report_dir, exist_ok=True)

    try:
        client.initialize()
        client.ensure_page_selected()

        # Initially navigate to base URL (unless the first step is an explicit Visit/Navigate to another path)
        initial_url = base_url
        if actions:
            first_action = actions[0]
            # Try to extract the URL or path if the action indicates visiting or navigating
            match = re.search(r'(?:visitar|visit|navegar\s+a|navigate\s+to|go\s+to)\s+([^\s]+)', first_action, re.IGNORECASE)
            if match:
                path_or_url = match.group(1).strip('`"\'')
                if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
                    initial_url = path_or_url
                else:
                    base_url_clean = base_url.rstrip("/")
                    path_clean = path_or_url.lstrip("/")
                    initial_url = f"{base_url_clean}/{path_clean}"
        
        print(f"Initially navigating to: {initial_url}", flush=True)
        try:
            client.call_tool("navigate_page", {"url": initial_url, "type": "url"})
        except Exception as nav_err:
            print(f"Warning: Could not navigate initially to {initial_url}: {nav_err}", file=sys.stderr)

        # System prompt configuration
        system_instruction = f"""You are an Autonomous QA Agent (web-flow-agent). Your mission is to validate a test flow on a web application by interpreting a Markdown specification file.
You interact with the page by calling tools from the Chrome DevTools Model Context Protocol (MCP) server.

At each turn, you will be provided with:
- The Objective of the test flow.
- The Steps/Actions to validate.
- The current URL and page title.
- The accessibility snapshot (A11y tree) of the current page's DOM with element UIDs.
- Recent JS console logs and network requests.
- The history of actions you have already performed in this execution.
- The Base URL of the project (e.g., {base_url}). If an action says "Visit /products" or "Go to /products", you must navigate to the base URL + relative path (e.g., {base_url}/products).

You must analyze the state of the page and decide the best action to take. Your response MUST be a valid JSON object that strictly complies with this structure:
{{
  "thought": "Detailed explanation in English of what you observe on the page, which step of the flow you are validating, and what you decide to do next.",
  "action": "call_tool" | "success" | "fail",
  "tool_name": "navigate_page" | "click" | "fill" | "wait_for" | "evaluate_script" | "take_screenshot",
  "tool_arguments": {{
    // Specific arguments for the tool to call
  }},
  "message": "Message in English detailing the reason for success or failure (only if action is 'success' or 'fail')."
}}

Critical interaction rules:
1. To navigate: Use the 'navigate_page' tool with arguments {{"url": "<full_url>", "type": "url"}}.
2. To click: Use the 'click' tool with {{"uid": "<element_uid_from_the_latest_snapshot>"}}.
3. To type/fill: Use the 'fill' tool with {{"uid": "<element_uid>", "value": "<text_to_type>"}}.
4. To wait: Use the 'wait_for' tool with {{"timeout": 2000}}.
5. If you see a severe console error or a network request with status >= 400/500 that prevents completing the flow, or if the flow does not behave as specified (e.g., the purchase total does not update or the checkout message does not appear), you must select "action": "fail" and explain the issue in "message".
6. If you have successfully completed all steps listed in the Markdown specification, select "action": "success" and detail the confirmation in "message".
7. Only use UIDs that exist in the most recent snapshot provided to you.
"""

        max_turns = 25
        for turn in range(1, max_turns + 1):
            print(f"--- Turn {turn}/{max_turns} ---", flush=True)

            # Get page state
            pages_text = client.call_tool_text("list_pages", {})
            pages = parse_pages(pages_text)
            current_url = "unknown"
            for p in pages:
                if p["selected"]:
                    current_url = p["url"]
                    break

            # Take DOM snapshot
            dom_snapshot = client.call_tool_text("take_snapshot", {})

            # Get diagnostics (console and network)
            try:
                console_logs = client.call_tool_text("list_console_messages", {"pageSize": 20})
            except Exception:
                console_logs = "Not available"

            try:
                network_requests = client.call_tool_text("list_network_requests", {"pageSize": 20})
            except Exception:
                network_requests = "Not available"

            # Compile content prompt
            user_content = {
                "objective": objective,
                "actions": actions,
                "current_url": current_url,
                "dom_snapshot": dom_snapshot,
                "console_logs": console_logs,
                "network_requests": network_requests,
                "history": history
            }

            contents = [
                {"role": "user", "parts": [{"text": json.dumps(user_content, indent=2)}]}
            ]

            print("Querying Gemini...", flush=True)
            response = call_gemini(api_key, system_instruction, contents)

            thought = response.get("thought", "")
            action = response.get("action", "")
            
            print(f"Agent Thought: {thought}", flush=True)
            
            if action == "call_tool":
                tool_name = response.get("tool_name")
                tool_args = response.get("tool_arguments", {})
                
                print(f"Executing tool: {tool_name} with arguments: {tool_args}", flush=True)
                
                try:
                    tool_result = client.call_tool_text(tool_name, tool_args)
                    print(f"Tool result: {tool_result[:200]}...", flush=True)
                    
                    # Short wait for the page to react/render before capturing the screenshot
                    time.sleep(1.5)
                    
                    history_entry = {
                        "turn": turn,
                        "thought": thought,
                        "action": f"Call to {tool_name}",
                        "arguments": tool_args,
                        "result": tool_result[:500] # Limit saved size
                    }
                    
                    # Screenshot of this step
                    step_ss_name = f"step_{turn}_{tool_name}.png"
                    step_ss_path = os.path.join(report_dir, step_ss_name)
                    try:
                        client.call_tool("take_screenshot", {"filePath": step_ss_path})
                        history_entry["screenshot"] = step_ss_name
                        print(f"Step {turn} screenshot saved to: {step_ss_name}", flush=True)
                    except Exception as ss_err:
                        print(f"Warning: Could not take screenshot of step {turn}: {ss_err}", file=sys.stderr)
                        
                    history.append(history_entry)
                except Exception as tool_err:
                    print(f"Error executing tool: {tool_err}", file=sys.stderr)
                    history.append({
                        "turn": turn,
                        "thought": thought,
                        "action": f"Call to {tool_name}",
                        "arguments": tool_args,
                        "error": str(tool_err)
                    })
                    time.sleep(1.5)

            elif action == "success":
                status = "success"
                final_message = response.get("message", "The flow completed successfully.")
                print(f"\nSUCCESS: {final_message}", flush=True)
                break

            elif action == "fail":
                status = "fail"
                final_message = response.get("message", "The agent reported a failure.")
                RED = "\033[91m"
                RESET = "\033[0m"
                print(f"\n{RED}FAILURE DETECTED: {final_message}{RESET}", flush=True)
                break
            else:
                print(f"Unknown action returned by Gemini: {action}", file=sys.stderr)
                status = "error"
                final_message = f"Unknown action: {action}"
                break

        if status == "running":
            status = "fail"
            final_message = f"The agent reached the limit of {max_turns} turns without achieving the objective."
            RED = "\033[91m"
            RESET = "\033[0m"
            print(f"\n{RED}FAILURE: {final_message}{RESET}", flush=True)

    except Exception as e:
        status = "error"
        final_message = f"{e}"
        RED = "\033[91m"
        RESET = "\033[0m"
        print(f"\n{RED}ERROR: {final_message}{RESET}", file=sys.stderr)
        
        # Save detailed error log
        error_log_path = os.path.join(report_dir, "error.log")
        try:
            with open(error_log_path, "w", encoding="utf-8") as f_err:
                f_err.write(f"Error: {final_message}\n\n")
                traceback.print_exc(file=f_err)
            print(f"Error log saved to: {error_log_path}", flush=True)
            report_data["error_log_file"] = error_log_path
        except Exception as log_err:
            print(f"Could not save error log file: {log_err}", file=sys.stderr)

    # --- Post-Mortem Diagnostics and Reports Phase ---
    print("\nGenerating reports...", flush=True)
    report_data.update({
        "spec": spec_path,
        "objective": objective,
        "status": status,
        "message": final_message,
        "history": history
    })

    try:
        # Try to get final technical diagnostics
        console_logs = client.call_tool_text("list_console_messages", {"pageSize": 50})
        network_requests = client.call_tool_text("list_network_requests", {"pageSize": 50})
        dom_snapshot = client.call_tool_text("take_snapshot", {})
        
        report_data["diagnostics"] = {
            "console_logs": console_logs,
            "network_requests": network_requests
        }

        # Save accessibility snapshot to file
        snapshot_path = os.path.join(report_dir, "snapshot.txt")
        with open(snapshot_path, "w", encoding="utf-8") as f:
            f.write(dom_snapshot)
        report_data["snapshot_file"] = snapshot_path

        # Save screenshot to file
        screenshot_path = os.path.join(report_dir, "screenshot.png")
        client.call_tool("take_screenshot", {"filePath": screenshot_path})
        report_data["screenshot_file"] = screenshot_path
        print(f"Screenshot saved to: {screenshot_path}", flush=True)

    except Exception as diag_err:
        print(f"Could not collect all final diagnostics: {diag_err}", file=sys.stderr)

    # Write final JSON report
    report_json_path = os.path.join(report_dir, "report.json")
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)
    print(f"JSON report saved to: {report_json_path}", flush=True)

    # Generate detailed Markdown report (report.md)
    report_md_path = os.path.join(report_dir, "report.md")
    try:
        status_color = "🟢" if status == "success" else "🔴"
        status_label = "SUCCESS" if status == "success" else "FAILURE / ERROR"
        
        md_content = []
        md_content.append(f"# QA Execution Report: {flow_name}\n")
        md_content.append(f"- **Status:** {status_color} **{status_label}**")
        md_content.append(f"- **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        md_content.append(f"- **Base URL:** [{base_url}]({base_url})")
        md_content.append(f"- **Specification:** `{spec_path}`\n")
        
        md_content.append("## Objective")
        md_content.append(f"{objective}\n")
        
        md_content.append("## Completion Message")
        md_content.append(f"> {final_message}\n")
        
        md_content.append("## Turn History\n")
        for turn_data in history:
            turn_num = turn_data.get("turn")
            thought = turn_data.get("thought", "").strip()
            action = turn_data.get("action", "").strip()
            args = turn_data.get("arguments", {})
            result = turn_data.get("result", "").strip()
            
            md_content.append(f"### Turn {turn_num}")
            md_content.append(f"**Thought:** {thought}\n")
            md_content.append(f"**Action:** `{action}` with arguments `{args}`\n")
            
            # Truncate result if it is too long (like snapshots) to keep MD readable
            if len(result) > 500:
                result_disp = result[:500] + "\n... (result truncated for readability, see full snapshot file)"
            else:
                result_disp = result
            
            md_content.append(f"**Result:**\n```\n{result_disp}\n```\n")
            
            # If screenshot is available for this step, include it
            if "screenshot" in turn_data:
                md_content.append(f"📸 **Step Screenshot:**\n![Step {turn_num}](./{turn_data['screenshot']})\n")
                
            md_content.append("---")
            
        md_content.append("\n## Diagnostics and Attachments")
        md_content.append("- 📊 [Complete JSON Report](./report.json)")
        md_content.append("- 📸 [Final Screenshot](./screenshot.png)")
        md_content.append("- 📄 [DOM Structural Snapshot (A11y)](./snapshot.txt)")
        if "error_log_file" in report_data:
            md_content.append("- 🛑 [Stack Error Log (error.log)](./error.log)")
            
        with open(report_md_path, "w", encoding="utf-8") as f_md:
            f_md.write("\n".join(md_content))
        print(f"Detailed Markdown report saved to: {report_md_path}", flush=True)
    except Exception as md_err:
        print(f"Could not write Markdown report: {md_err}", file=sys.stderr)

    # Cleanup
    client.close()

    # Exit with corresponding code
    if status == "success":
        sys.exit(0)
    else:
        sys.exit(1)

def load_dotenv(dotenv_path=".env"):
    if os.path.exists(dotenv_path):
        try:
            with open(dotenv_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].strip()
                        if val.startswith('"') and val.endswith('"'):
                            val = val[1:-1]
                        elif val.startswith("'") and val.endswith("'"):
                            val = val[1:-1]
                        if key not in os.environ:
                            os.environ[key] = val
        except Exception as e:
            print(f"Warning: Could not read {dotenv_path}: {e}", file=sys.stderr)

if __name__ == "__main__":
    load_dotenv()
    
    parser = argparse.ArgumentParser(description="Antigravity 2.0 QA Autonomous Sidecar Agent")
    parser.add_argument("--run", action="store_true", help="Run the QA agent loop")
    parser.add_argument("--spec", required=True, help="Path to the Markdown specification file")
    parser.add_argument("--base-url", default="http://localhost:3000", help="Base URL of the project to test")
    
    args = parser.parse_args()
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable is not set.", file=sys.stderr)
        print("Please set it in your .env file or export it before running.", file=sys.stderr)
        sys.exit(1)
        
    run_qa_agent(args.spec, args.base_url, api_key)
