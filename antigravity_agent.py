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
        
        # Valores por defecto para iniciar el servidor MCP
        cmd = "npx"
        args = ["-y", "chrome-devtools-mcp"]
        env = os.environ.copy()
        
        # Intentar cargar configuración local desde .agents/mcp_config.json
        config_path = os.path.join(".agents", "mcp_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                    server_config = config_data.get("mcpServers", {}).get("chrome-devtools", {})
                    if server_config:
                        cmd = server_config.get("command", cmd)
                        args = list(server_config.get("args", args))
                        # Combinar variables de entorno
                        custom_env = server_config.get("env", {})
                        for k, v in custom_env.items():
                            env[str(k)] = str(v)
                        print(f"Cargada configuración de MCP desde {config_path}", flush=True)
            except Exception as e:
                print(f"Advertencia: No se pudo leer {config_path}: {e}", file=sys.stderr)

        user_data_path = os.path.abspath(".agents/chrome-profile")
        # Asegurar que el perfil de Chrome esté configurado de forma aislada si no se especificó otro en los argumentos
        has_user_data_dir = any(arg.startswith("--user-data-dir") for arg in args)
        if not has_user_data_dir:
            args.append(f"--user-data-dir={user_data_path}")

        print(f"Iniciando servidor MCP: {cmd} {' '.join(args)}", flush=True)
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
                raise RuntimeError("El servidor MCP cerró la conexión inesperadamente.")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Omitir líneas que no sean JSON si las hay
                continue
            
            # Verificar si es la respuesta a nuestra petición
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"Error MCP: {msg['error']}")
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
        print("Servidor MCP inicializado correctamente.", flush=True)
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
            print("No se encontraron pestañas abiertas. Abriendo una nueva...", flush=True)
            self.call_tool("new_page", {"url": "about:blank"})
            time.sleep(1) # Esperar a que se abra
        else:
            has_selected = any(p["selected"] for p in pages)
            if not has_selected:
                print(f"Seleccionando la pestaña por defecto: ID {pages[0]['id']}", flush=True)
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
        raise FileNotFoundError(f"No se encontró el archivo de especificación: {spec_path}")
        
    with open(spec_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("- **Objetivo:**") or line.startswith("- **Objetivo**"):
                objective = line.replace("- **Objetivo:**", "").replace("- **Objetivo**", "").strip()
            elif line.startswith("- **Acción:**") or line.startswith("- **Acción**"):
                action = line.replace("- **Acción:**", "").replace("- **Acción**", "").strip()
                actions.append(action)
            elif line.startswith("- ") and ("Objetivo:" in line or "Objetivo" in line):
                parts = line.split(":", 1)
                objective = parts[1].strip()
            elif line.startswith("- ") and ("Acción:" in line or "Acción" in line):
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
                    
    # Fallback si no se cerraron las llaves de forma balanceada
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
            message = err_json.get("error", {}).get("message", "Error de red desconocido")
        except Exception:
            message = err_msg
        raise RuntimeError(f"Error en Gemini API ({e.code}): {message}") from e
    except Exception as e:
        raise RuntimeError(f"Error al realizar petición a Gemini: {e}") from e

    try:
        cleaned = clean_json_text(text)
        return json.loads(cleaned)
    except Exception as e:
        print(f"Error parseando respuesta de Gemini. Texto crudo recibido:\n{text}", file=sys.stderr)
        raise RuntimeError(f"Error al parsear respuesta JSON de Gemini: {e}. Texto crudo: {text[:200]}") from e

def run_qa_agent(spec_path, base_url, api_key):
    # Parsear especificación
    objective, actions = parse_spec(spec_path)
    print("\n" + "="*50)
    print(f"INICIANDO QA AGENT")
    print(f"Especificación: {spec_path}")
    print(f"Objetivo: {objective}")
    print(f"URL Base: {base_url}")
    print("="*50 + "\n")

    # Iniciar cliente MCP
    client = McpClient()
    history = []
    status = "running"
    final_message = ""
    
    # Creamos un nombre limpio para los reportes
    flow_name = os.path.splitext(os.path.basename(spec_path))[0]
    
    # Limpiamos la URL para el nombre de archivo del reporte (report_[flow]_[url_auditada]_[date]_[time])
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

        # Navegar inicialmente a la URL base (salvo que el primer paso sea un Visitar/Navegar explícito a otra ruta)
        initial_url = base_url
        if actions:
            first_action = actions[0]
            # Intentamos extraer la URL o ruta si la acción indica visitar o navegar
            match = re.search(r'(?:visitar|navegar\s+a)\s+([^\s]+)', first_action, re.IGNORECASE)
            if match:
                path_or_url = match.group(1).strip('`"\'')
                if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
                    initial_url = path_or_url
                else:
                    base_url_clean = base_url.rstrip("/")
                    path_clean = path_or_url.lstrip("/")
                    initial_url = f"{base_url_clean}/{path_clean}"
        
        print(f"Navegando inicialmente a: {initial_url}", flush=True)
        try:
            client.call_tool("navigate_page", {"url": initial_url, "type": "url"})
        except Exception as nav_err:
            print(f"Advertencia: No se pudo navegar inicialmente a {initial_url}: {nav_err}", file=sys.stderr)

        # Configuración del prompt del sistema
        system_instruction = f"""Eres un Agente Autónomo de QA (web-flow-agent). Tu misión es validar un flujo de prueba sobre una aplicación web interpretando un archivo de especificaciones en Markdown.
Interactúas con la página llamando a herramientas del Model Context Protocol (MCP) de Chrome DevTools.

En cada turno se te proporcionará:
- El Objetivo del flujo de prueba.
- Los Pasos/Acciones a validar.
- La URL actual y título de la página.
- El snapshot del árbol de accesibilidad (A11y tree) del DOM de la página actual con los UIDs de los elementos.
- Los logs recientes de la consola de JS y peticiones de red.
- El historial de acciones que ya has realizado en esta ejecución.
- La URL Base del proyecto (e.g., {base_url}). Si una acción dice "Visitar /productos", debes navegar a la URL base + ruta relativa (ej. {base_url}/productos).

Debes analizar el estado de la página y decidir cuál es la mejor acción a realizar. Tu respuesta DEBE ser un JSON válido que cumpla estrictamente con esta estructura:
{{
  "thought": "Explicación detallada en español sobre qué observas en la página, qué paso del flujo estás validando y qué decides hacer a continuación.",
  "action": "call_tool" | "success" | "fail",
  "tool_name": "navigate_page" | "click" | "fill" | "wait_for" | "evaluate_script" | "take_screenshot",
  "tool_arguments": {{
    // Argumentos específicos de la herramienta a llamar
  }},
  "message": "Mensaje en español detallando la razón del éxito o del fallo (solo si action es 'success' o 'fail')."
}}

Reglas críticas de interacción:
1. Para navegar: Usa la herramienta 'navigate_page' con los argumentos {{"url": "<url_completa>", "type": "url"}}.
2. Para hacer clic: Usa la herramienta 'click' con {{"uid": "<uid_del_elemento_del_último_snapshot>"}}.
3. Para escribir: Usa la herramienta 'fill' con {{"uid": "<uid_del_elemento>", "value": "<texto_a_escribir>"}}.
4. Para esperar: Usa la herramienta 'wait_for' con {{"timeout": 2000}}.
5. Si ves un error grave de consola o una petición de red con código >= 400/500 que impida completar el flujo, o si el flujo no se comporta como se especifica (ej. el total de la compra no se actualiza o no aparece el mensaje de checkout), debes seleccionar "action": "fail" y explicar el problema en "message".
6. Si has completado con éxito todos los pasos indicados en la especificación Markdown, selecciona "action": "success" y detalla la confirmación en "message".
7. Solo usa UIDs que existan en el snapshot más reciente que se te ha proporcionado.
"""

        max_turns = 25
        for turn in range(1, max_turns + 1):
            print(f"--- Turno {turn}/{max_turns} ---", flush=True)

            # Obtener estado de la página
            pages_text = client.call_tool_text("list_pages", {})
            pages = parse_pages(pages_text)
            current_url = "unknown"
            for p in pages:
                if p["selected"]:
                    current_url = p["url"]
                    break

            # Tomar snapshot del DOM
            dom_snapshot = client.call_tool_text("take_snapshot", {})

            # Obtener diagnósticos (consola y red)
            try:
                console_logs = client.call_tool_text("list_console_messages", {"pageSize": 20})
            except Exception:
                console_logs = "No disponible"

            try:
                network_requests = client.call_tool_text("list_network_requests", {"pageSize": 20})
            except Exception:
                network_requests = "No disponible"

            # Compilar prompt de contenido
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

            print("Consultando a Gemini...", flush=True)
            response = call_gemini(api_key, system_instruction, contents)

            thought = response.get("thought", "")
            action = response.get("action", "")
            
            print(f"Pensamiento del Agente: {thought}", flush=True)
            
            if action == "call_tool":
                tool_name = response.get("tool_name")
                tool_args = response.get("tool_arguments", {})
                
                print(f"Ejecutando herramienta: {tool_name} con argumentos: {tool_args}", flush=True)
                
                try:
                    tool_result = client.call_tool_text(tool_name, tool_args)
                    print(f"Resultado herramienta: {tool_result[:200]}...", flush=True)
                    
                    # Espera corta para que la página reaccione/renderice antes de la captura
                    time.sleep(1.5)
                    
                    history_entry = {
                        "turn": turn,
                        "thought": thought,
                        "action": f"Llamada a {tool_name}",
                        "arguments": tool_args,
                        "result": tool_result[:500] # Limitar tamaño guardado
                    }
                    
                    # Captura de pantalla de este paso
                    step_ss_name = f"step_{turn}_{tool_name}.png"
                    step_ss_path = os.path.join(report_dir, step_ss_name)
                    try:
                        client.call_tool("take_screenshot", {"filePath": step_ss_path})
                        history_entry["screenshot"] = step_ss_name
                        print(f"Captura de pantalla del paso {turn} guardada en: {step_ss_name}", flush=True)
                    except Exception as ss_err:
                        print(f"Advertencia: No se pudo tomar captura del paso {turn}: {ss_err}", file=sys.stderr)
                        
                    history.append(history_entry)
                except Exception as tool_err:
                    print(f"Error al ejecutar herramienta: {tool_err}", file=sys.stderr)
                    history.append({
                        "turn": turn,
                        "thought": thought,
                        "action": f"Llamada a {tool_name}",
                        "arguments": tool_args,
                        "error": str(tool_err)
                    })
                    time.sleep(1.5)

            elif action == "success":
                status = "success"
                final_message = response.get("message", "El flujo finalizó correctamente.")
                print(f"\n¡ÉXITO!: {final_message}", flush=True)
                break

            elif action == "fail":
                status = "fail"
                final_message = response.get("message", "El agente reportó un fallo.")
                RED = "\033[91m"
                RESET = "\033[0m"
                print(f"\n{RED}¡FALLO DETECTADO!: {final_message}{RESET}", flush=True)
                break
            else:
                print(f"Acción desconocida devuelta por Gemini: {action}", file=sys.stderr)
                status = "error"
                final_message = f"Acción desconocida: {action}"
                break

        if status == "running":
            status = "fail"
            final_message = f"El agente agotó el límite de {max_turns} turnos sin resolver el objetivo."
            RED = "\033[91m"
            RESET = "\033[0m"
            print(f"\n{RED}¡FALLO!: {final_message}{RESET}", flush=True)

    except Exception as e:
        status = "error"
        final_message = f"{e}"
        RED = "\033[91m"
        RESET = "\033[0m"
        print(f"\n{RED}ERROR: {final_message}{RESET}", file=sys.stderr)
        
        # Guardar log de error detallado
        error_log_path = os.path.join(report_dir, "error.log")
        try:
            with open(error_log_path, "w", encoding="utf-8") as f_err:
                f_err.write(f"Error: {final_message}\n\n")
                traceback.print_exc(file=f_err)
            print(f"Log de error guardado en: {error_log_path}", flush=True)
            report_data["error_log_file"] = error_log_path
        except Exception as log_err:
            print(f"No se pudo guardar el archivo de log de error: {log_err}", file=sys.stderr)

    # --- Fase de Diagnóstico Post-Mortem y Reportes ---
    print("\nGenerando reportes...", flush=True)
    report_data.update({
        "spec": spec_path,
        "objective": objective,
        "status": status,
        "message": final_message,
        "history": history
    })

    try:
        # Intentamos obtener diagnóstico técnico final
        console_logs = client.call_tool_text("list_console_messages", {"pageSize": 50})
        network_requests = client.call_tool_text("list_network_requests", {"pageSize": 50})
        dom_snapshot = client.call_tool_text("take_snapshot", {})
        
        report_data["diagnostics"] = {
            "console_logs": console_logs,
            "network_requests": network_requests
        }

        # Guardar snapshot de accesibilidad en archivo
        snapshot_path = os.path.join(report_dir, "snapshot.txt")
        with open(snapshot_path, "w", encoding="utf-8") as f:
            f.write(dom_snapshot)
        report_data["snapshot_file"] = snapshot_path

        # Guardar captura de pantalla en archivo
        screenshot_path = os.path.join(report_dir, "screenshot.png")
        client.call_tool("take_screenshot", {"filePath": screenshot_path})
        report_data["screenshot_file"] = screenshot_path
        print(f"Captura de pantalla guardada en: {screenshot_path}", flush=True)

    except Exception as diag_err:
        print(f"No se pudieron recopilar todos los diagnósticos finales: {diag_err}", file=sys.stderr)

    # Escribir reporte JSON final
    report_json_path = os.path.join(report_dir, "report.json")
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)
    print(f"Reporte JSON guardado en: {report_json_path}", flush=True)

    # Generar reporte Markdown detallado (report.md)
    report_md_path = os.path.join(report_dir, "report.md")
    try:
        status_color = "🟢" if status == "success" else "🔴"
        status_label = "ÉXITO" if status == "success" else "FALLO / ERROR"
        
        md_content = []
        md_content.append(f"# Reporte de Ejecución de QA: {flow_name}\n")
        md_content.append(f"- **Estado:** {status_color} **{status_label}**")
        md_content.append(f"- **Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        md_content.append(f"- **URL Base:** [{base_url}]({base_url})")
        md_content.append(f"- **Especificación:** `{spec_path}`\n")
        
        md_content.append("## Objetivo")
        md_content.append(f"{objective}\n")
        
        md_content.append("## Mensaje de Finalización")
        md_content.append(f"> {final_message}\n")
        
        md_content.append("## Historial de Turnos\n")
        for turn_data in history:
            turn_num = turn_data.get("turn")
            thought = turn_data.get("thought", "").strip()
            action = turn_data.get("action", "").strip()
            args = turn_data.get("arguments", {})
            result = turn_data.get("result", "").strip()
            
            md_content.append(f"### Turno {turn_num}")
            md_content.append(f"**Pensamiento:** {thought}\n")
            md_content.append(f"**Acción:** `{action}` con argumentos `{args}`\n")
            
            # Recortar el resultado si es muy largo (como snapshots) para mantener el MD legible
            if len(result) > 500:
                result_disp = result[:500] + "\n... (resultado truncado para legibilidad, ver archivo de snapshot completo)"
            else:
                result_disp = result
            
            md_content.append(f"**Resultado:**\n```\n{result_disp}\n```\n")
            
            # Si hay captura de pantalla para este paso, incluirla
            if "screenshot" in turn_data:
                md_content.append(f"📸 **Captura de pantalla del paso:**\n![Paso {turn_num}](./{turn_data['screenshot']})\n")
                
            md_content.append("---")
            
        md_content.append("\n## Diagnósticos y Archivos Adjuntos")
        md_content.append("- 📊 [Reporte JSON Completo](./report.json)")
        md_content.append("- 📸 [Captura de Pantalla Final](./screenshot.png)")
        md_content.append("- 📄 [Snapshot Estructural del DOM (A11y)](./snapshot.txt)")
        if "error_log_file" in report_data:
            md_content.append("- 🛑 [Log de Error de la Pila (error.log)](./error.log)")
            
        with open(report_md_path, "w", encoding="utf-8") as f_md:
            f_md.write("\n".join(md_content))
        print(f"Reporte Markdown detallado guardado en: {report_md_path}", flush=True)
    except Exception as md_err:
        print(f"No se pudo escribir el reporte Markdown: {md_err}", file=sys.stderr)

    # Limpieza
    client.close()

    # Salida con código correspondiente
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
            print(f"Advertencia: No se pudo leer {dotenv_path}: {e}", file=sys.stderr)

if __name__ == "__main__":
    load_dotenv()
    
    parser = argparse.ArgumentParser(description="Antigravity 2.0 QA Autonomous Sidecar Agent")
    parser.add_argument("--run", action="store_true", help="Ejecutar el bucle de agente de QA")
    parser.add_argument("--spec", required=True, help="Ruta al archivo de especificación Markdown")
    parser.add_argument("--base-url", default="http://localhost:3000", help="URL base del proyecto a probar")
    
    args = parser.parse_args()
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: La variable de entorno GEMINI_API_KEY no está configurada.", file=sys.stderr)
        print("Por favor, configúrala en el archivo .env o expórtala antes de ejecutar.", file=sys.stderr)
        sys.exit(1)
        
    run_qa_agent(args.spec, args.base_url, api_key)
