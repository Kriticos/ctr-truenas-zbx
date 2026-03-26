import os
import ssl
import json
import time
import subprocess
import websocket


TRUENAS_HOST = os.getenv("TRUENAS_HOST", "").strip()
TRUENAS_API_KEY = os.getenv("TRUENAS_API_KEY", "").strip()
TRUENAS_WS_PATH = os.getenv("TRUENAS_WS_PATH", "/api/current").strip()
TRUENAS_VERIFY_SSL = os.getenv("TRUENAS_VERIFY_SSL", "true").strip().lower() == "true"

ZABBIX_SERVER = os.getenv("ZABBIX_SERVER", "").strip()
ZABBIX_PORT = os.getenv("ZABBIX_PORT", "10051").strip()
ZABBIX_HOST = os.getenv("ZABBIX_HOST", "").strip()
ZABBIX_KEY = os.getenv("ZABBIX_KEY", "truenas.raw").strip()

COLLECT_INTERVAL = int(os.getenv("COLLECT_INTERVAL", "300").strip())


def ws_url() -> str:
    return f"wss://{TRUENAS_HOST}{TRUENAS_WS_PATH}"


def make_ws():
    sslopt = {}
    if not TRUENAS_VERIFY_SSL:
        sslopt = {"cert_reqs": ssl.CERT_NONE}
    return websocket.create_connection(ws_url(), sslopt=sslopt, timeout=30)


def rpc_call(ws, method: str, params=None, request_id: int = 1):
    if params is None:
        params = []

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }

    ws.send(json.dumps(payload))
    raw = ws.recv()
    data = json.loads(raw)

    if "error" in data:
        raise RuntimeError(
            f"Erro RPC em {method}: {json.dumps(data['error'], ensure_ascii=False)}"
        )

    return data.get("result")


def build_temperature_lookup(temperatures_result) -> dict:
    if isinstance(temperatures_result, dict):
        return temperatures_result
    return {}


def should_include_disk(disk: dict) -> bool:
    model = (disk.get("model") or "").upper()
    if "QEMU" in model:
        return False
    return True


def normalize_disk(disk: dict, temp_lookup: dict) -> dict:
    disk_name = disk.get("name") or disk.get("devname") or disk.get("identifier")

    return {
        "name": disk_name,
        "model": disk.get("model"),
        "serial": disk.get("serial"),
        "temperature": temp_lookup.get(disk_name),
    }


def extract_numeric(value):
    """
    Tenta extrair número de formatos variados do TrueNAS, por exemplo:
    - 123
    - 123.0
    - "123"
    - {"parsed": 123}
    - {"value": 123}
    - {"rawvalue": "123"}
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return value

    if isinstance(value, str):
        try:
            if "." in value:
                return float(value)
            return int(value)
        except Exception:
            return None

    if isinstance(value, dict):
        for key in ("parsed", "value", "rawvalue", "number"):
            if key in value:
                return extract_numeric(value[key])

    return None


def collect_root_datasets(ws, pool_names: list[str]):
    if not pool_names:
        return []

    filters = [["id", "in", pool_names]]
    options = {}

    return rpc_call(
        ws,
        "pool.dataset.query",
        [filters, options],
        request_id=7
    )


def build_dataset_lookup(root_datasets: list[dict]) -> dict:
    lookup = {}

    for ds in root_datasets:
        ds_id = ds.get("id")
        if not ds_id:
            continue

        # Tenta vários candidatos comuns para espaço usado/livre em datasets.
        used_candidates = [
            ds.get("used"),
            ds.get("used_bytes"),
            ds.get("usedbydataset"),
            ds.get("usedbydataset_bytes"),
        ]

        free_candidates = [
            ds.get("available"),
            ds.get("available_bytes"),
            ds.get("free"),
            ds.get("free_bytes"),
        ]

        used_value = None
        free_value = None

        for candidate in used_candidates:
            used_value = extract_numeric(candidate)
            if used_value is not None:
                break

        for candidate in free_candidates:
            free_value = extract_numeric(candidate)
            if free_value is not None:
                break

        usable_total = None
        if used_value is not None and free_value is not None:
            usable_total = used_value + free_value

        lookup[ds_id] = {
            "dataset_id": ds_id,
            "used": used_value,
            "free": free_value,
            "usable_total": usable_total,
            "raw": ds,
        }

    return lookup


def normalize_pool(pool: dict, dataset_lookup: dict) -> dict:
    pool_name = pool.get("name")
    dataset_info = dataset_lookup.get(pool_name, {})

    usable_total = dataset_info.get("usable_total")
    dataset_used = dataset_info.get("used")
    dataset_free = dataset_info.get("free")

    # fallback para pool.query se o dataset não entregar os campos esperados
    return {
        "name": pool_name,
        "total": usable_total if usable_total is not None else pool.get("size"),
        "used": dataset_used if dataset_used is not None else pool.get("allocated"),
        "free": dataset_free if dataset_free is not None else pool.get("free"),
        "pool_query_total": pool.get("size"),
        "pool_query_used": pool.get("allocated"),
        "pool_query_free": pool.get("free"),
    }


def collect_data() -> dict:
    if not TRUENAS_HOST:
        raise ValueError("TRUENAS_HOST não definido no .env")
    if not TRUENAS_API_KEY:
        raise ValueError("TRUENAS_API_KEY não definido no .env")

    print(f"Conectando em: {ws_url()}", flush=True)
    ws = make_ws()

    try:
        auth_result = rpc_call(
            ws,
            "auth.login_with_api_key",
            [TRUENAS_API_KEY],
            request_id=1
        )
        print(f"AUTH RESULT: {auth_result}", flush=True)

        version = rpc_call(ws, "system.version", [], request_id=2)
        hostname = rpc_call(ws, "system.hostname", [], request_id=3)
        pools_raw = rpc_call(ws, "pool.query", [], request_id=4)
        disks_raw = rpc_call(ws, "disk.query", [], request_id=5)

        pool_names = [pool.get("name") for pool in pools_raw if pool.get("name")]
        root_datasets = collect_root_datasets(ws, pool_names)
        dataset_lookup = build_dataset_lookup(root_datasets)

        included_disks = [
            disk for disk in disks_raw
            if (disk.get("name") or disk.get("devname")) and should_include_disk(disk)
        ]

        disk_names = []
        for disk in included_disks:
            disk_name = disk.get("name") or disk.get("devname")
            if disk_name:
                disk_names.append(disk_name)

        disk_names = sorted(set(disk_names))

        temp_lookup = {}
        if disk_names:
            try:
                temperatures_raw = rpc_call(
                    ws,
                    "disk.temperatures",
                    [disk_names, False],
                    request_id=6
                )
                temp_lookup = build_temperature_lookup(temperatures_raw)
            except Exception as e:
                print(f"Falha ao coletar temperaturas: {e}", flush=True)

        pools_filtered = [
            normalize_pool(pool, dataset_lookup)
            for pool in pools_raw
        ]

        disks_filtered = [
            normalize_disk(disk, temp_lookup)
            for disk in included_disks
        ]

        # bloco de debug para você validar o que o dataset raiz trouxe
        dataset_debug = {}
        for pool_name, ds in dataset_lookup.items():
            dataset_debug[pool_name] = {
                "dataset_id": ds.get("dataset_id"),
                "used": ds.get("used"),
                "free": ds.get("free"),
                "usable_total": ds.get("usable_total"),
            }

        result = {
            "hostname": hostname,
            "version": version,
            "pool_count": len(pools_filtered),
            "pools": pools_filtered,
            "disk_count": len(disks_filtered),
            "disks": disks_filtered,
            "dataset_debug": dataset_debug,
        }

        return result

    finally:
        ws.close()


def send_to_zabbix(payload: dict):
    if not ZABBIX_SERVER:
        raise ValueError("ZABBIX_SERVER não definido no .env")
    if not ZABBIX_HOST:
        raise ValueError("ZABBIX_HOST não definido no .env")

    json_payload = json.dumps(payload, ensure_ascii=False)

    print("\nJSON FINAL", flush=True)
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), flush=True)

    cmd = [
        "zabbix_sender",
        "-z", ZABBIX_SERVER,
        "-p", ZABBIX_PORT,
        "-s", ZABBIX_HOST,
        "-k", ZABBIX_KEY,
        "-o", json_payload
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False
    )

    print("\nZABBIX_SENDER STDOUT", flush=True)
    print(result.stdout.strip(), flush=True)

    if result.stderr.strip():
        print("\nZABBIX_SENDER STDERR", flush=True)
        print(result.stderr.strip(), flush=True)

    if result.returncode != 0:
        raise RuntimeError(f"Falha no zabbix_sender. Código: {result.returncode}")


def run_once():
    payload = collect_data()
    send_to_zabbix(payload)


def main():
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Erro na coleta/envio: {e}", flush=True)

        print(f"Aguardando {COLLECT_INTERVAL} segundos para próxima coleta...", flush=True)
        time.sleep(COLLECT_INTERVAL)


if __name__ == "__main__":
    main()