#!/bin/bash
# run.sh - Orquestador de agentes de QA

# Detener si hay errores en comandos críticos
set -e

# Inicializar variable vacía para la URL y el selector de flujo
BASE_URL=""
FLOW_SELECT=""

# Parsea argumentos
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --url) BASE_URL="$2"; shift ;;
    --flow) FLOW_SELECT="$2"; shift ;;
    *) echo "Opción desconocida: $1"; exit 1 ;;
  esac
  shift
done

# Validar URL
if [ -z "$BASE_URL" ]; then
  echo "Error: El parámetro --url es obligatorio."
  echo "Uso: ./run.sh --url <URL_del_proyecto> [--flow <nombre_o_ruta_de_flujo>]"
  echo "Ejemplo: ./run.sh --url http://localhost:3000 --flow ejemplo_flujo.md"
  exit 1
fi

# Cargar variables desde archivo .env si existe
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    # Omitir comentarios y líneas vacías
    [[ "$line" =~ ^#.*$ ]] && continue
    [[ -z "$line" ]] && continue
    key=$(echo "$line" | cut -d '=' -f 1 | xargs)
    val=$(echo "$line" | cut -d '=' -f 2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
    export "$key=$val"
  done < .env
fi

# Validar API key
if [ -z "$GEMINI_API_KEY" ]; then
  echo "Error: La variable de entorno GEMINI_API_KEY no está configurada."
  echo "Por favor configúrala en el archivo .env o expórtala en la terminal."
  exit 1
fi

# Buscar flujos disponibles (.md)
flows=()
for f in ./flows/*.md; do
  [ -f "$f" ] && flows+=("$f")
done

if [ ${#flows[@]} -eq 0 ]; then
  echo "Error: No se encontraron archivos de flujo (.md) en la carpeta ./flows"
  exit 1
fi

# Determinar qué flujos ejecutar
flows_to_run=()
if [ -n "$FLOW_SELECT" ]; then
  if [ -f "$FLOW_SELECT" ]; then
    flows_to_run+=("$FLOW_SELECT")
  elif [ -f "./flows/$FLOW_SELECT" ]; then
    flows_to_run+=("./flows/$FLOW_SELECT")
  else
    echo "Error: El flujo especificado '$FLOW_SELECT' no existe."
    exit 1
  fi
else
  echo "=================================================="
  echo "Selecciona el flujo de QA que deseas ejecutar:"
  echo "0) [Ejecutar todos los flujos]"
  for i in "${!flows[@]}"; do
    echo "$((i+1))) $(basename "${flows[$i]}")"
  done
  echo "=================================================="
  read -p "Elige una opción (0-${#flows[@]}) [0]: " choice
  choice=${choice:-0}
  
  if [ "$choice" -eq 0 ] 2>/dev/null; then
    flows_to_run=("${flows[@]}")
  else
    idx=$((choice-1))
    if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#flows[@]}" ]; then
      flows_to_run+=("${flows[$idx]}")
    else
      echo "Opción inválida."
      exit 1
    fi
  fi
fi

echo "=================================================="
echo "Iniciando Suite de Pruebas Autónomas de QA"
echo "URL Objetivo: $BASE_URL"
echo "=================================================="

# Crear directorios necesarios
mkdir -p ./reports
mkdir -p ./flows

# Bandera para rastrear fallas (no usar set -e para la iteración para que continúe)
set +e
FAILED=0

# Iterar sobre los flujos seleccionados
for flow in "${flows_to_run[@]}"; do
  echo ""
  echo "--------------------------------------------------"
  echo "Ejecutando flujo: $(basename "$flow")"
  echo "--------------------------------------------------"
  
  # Ejecuta el runner de Python
  python3 ./antigravity_agent.py --run --spec "$flow" --base-url "$BASE_URL"
  RESULT=$?
  
  # Definir colores para la salida
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  NC='\033[0m' # Sin color

  if [ $RESULT -eq 0 ]; then
    echo -e ">> RESULTADO: ${GREEN}[ÉXITO] $(basename "$flow")${NC}"
  else
    echo -e ">> RESULTADO: ${RED}[FALLO] $(basename "$flow")${NC} (Código de salida: $RESULT)"
    FAILED=1
  fi
done

echo ""
echo "=================================================="
if [ "$FAILED" -eq 0 ]; then
  echo -e "${GREEN}PROCESO COMPLETADO: Todos los flujos seleccionados se ejecutaron con ÉXITO.${NC}"
  exit 0
else
  echo -e "${RED}PROCESO COMPLETADO con ERRORES. Algunos flujos fallaron.${NC}"
  echo "Por favor revisa la carpeta ./reports para los detalles del diagnóstico y logs de error."
  exit 1
fi
