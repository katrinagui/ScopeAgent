"""Optional VISA identity discovery. Only *IDN? is sent to enumerated/user-selected resources."""
import ipaddress
import re

MAX_DEVICES = 8
MSO5000_CHANNELS = {"MSO5072": 2, "MSO5074": 4, "MSO5102": 2,
                    "MSO5104": 4, "MSO5204": 4, "MSO5354": 4}


def normalize_resource(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise ValueError("请输入示波器 IP 或完整的 LAN / USB VISA INSTR 地址。")
    value = value.strip()
    try:
        return f"TCPIP::{ipaddress.IPv4Address(value)}::INSTR"
    except ipaddress.AddressValueError:
        pass
    match = re.fullmatch(r"TCPIP\d*::([^:]+)::(?:(inst\d+)::)?INSTR", value, re.IGNORECASE)
    if match:
        host = match[1]
        if re.fullmatch(r"[0-9.]+", host):
            try:
                ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError as exc:
                raise ValueError("示波器 IPv4 地址无效。") from exc
        elif not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", p)
                     for p in host.rstrip(".").split(".")):
            raise ValueError("VISA 地址中的主机名无效。")
        return value
    if re.fullmatch(r"USB\d*::(?:0x[0-9a-f]+|\d+)::(?:0x[0-9a-f]+|\d+)::[a-z0-9_.-]+(?:::\d+)?::INSTR", value, re.IGNORECASE):
        return value
    raise ValueError("仅支持 IPv4、TCPIP 主机 INSTR 或完整 USB INSTR 地址，不接受网页 URL / SOCKET / SCPI。")


def capabilities(vendor, model):
    if "RIGOL" in vendor.upper() and model.upper() in MSO5000_CHANNELS:
        # ponytail: base model channels only; licensed upgrades need a verified option query.
        return {"driver": "rigol_mso5000", "supported": True, "views": ["curve", "screen"],
                "channels": list(range(1, MSO5000_CHANNELS[model.upper()] + 1)),
                "message": "已匹配 MSO5000 适配器；波形与截图的实机兼容性仍需验证。"}
    return {"driver": None, "supported": False, "views": [], "channels": [],
            "message": "已读取设备身份，但当前没有该型号的波形 / 截图适配器，不能开始监看。可导出 CSV 后离线分析。"}


def visa_manager():
    try:
        import pyvisa
    except ImportError as exc:
        raise ValueError("当前服务环境没有 PyVISA。请按使用说明准备硬件环境并安装 requirements-hardware.txt，再用该环境重启；演示仍可使用。") from exc
    try:
        return pyvisa.ResourceManager()
    except Exception as exc:
        raise ValueError("无法初始化 VISA 后端，请检查 PyVISA / PyVISA-py 或已配置的 VISA 驱动。") from exc


def read_identity(manager, resource):
    device = manager.open_resource(resource, timeout=1000, read_termination="\n", write_termination="\n")
    try:
        reply = device.query("*IDN?")
        if not isinstance(reply, str) or not 1 <= len(reply) <= 4096:
            raise ValueError("设备身份响应为空或过长。")
        reply = reply.strip()
        if any(ord(c) < 32 for c in reply):
            raise ValueError("设备身份响应包含非预期控制字符。")
        parts = [part.strip() for part in reply.split(",")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError("设备未返回可识别的厂商与型号。")
        vendor, model = parts[:2]
        profile = capabilities(vendor, model)
        if profile["supported"] and len(parts) != 4:
            profile = {"driver": None, "supported": False, "views": [], "channels": [],
                       "message": "设备身份字段不符合当前适配器的四字段契约，暂不能监看。"}
        return {"resource": resource, "vendor": vendor, "model": model,
                "serial": parts[2] if len(parts) > 2 else "",
                "firmware": ",".join(parts[3:]), **profile}
    finally:
        device.close()


def discover(manager_factory=visa_manager):
    manager = manager_factory()
    try:
        resources = []
        for raw in sorted(set(manager.list_resources("?*::INSTR"))):
            try:
                resource = normalize_resource(raw)
            except ValueError:
                continue
            if resource not in resources:
                resources.append(resource)
        devices = []
        for resource in resources[:MAX_DEVICES]:
            try:
                devices.append(read_identity(manager, resource))
            except Exception as exc:
                devices.append({"resource": resource, "vendor": "", "model": "身份读取失败", "serial": "",
                    "firmware": "", "driver": None, "supported": False, "views": [], "channels": [],
                    "message": f"未能读取设备身份（{type(exc).__name__}），请核对地址、连接与设备占用。"})
        return {"devices": devices, "truncated": len(resources) > MAX_DEVICES,
                "message": f"已检查 {len(devices)} 个 VISA 资源。扫描发现可能不完整；未出现的设备可手动填写地址后识别。"}
    except Exception as exc:
        raise ValueError(f"VISA 资源扫描失败（{type(exc).__name__}），请检查后端；也可尝试手动识别地址。") from exc
    finally:
        manager.close()


def identify(resource, manager_factory=visa_manager):
    resource = normalize_resource(resource)
    manager = manager_factory()
    try:
        return read_identity(manager, resource)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"读取设备身份失败（{type(exc).__name__}），请核对地址、后端和设备占用。") from exc
    finally:
        manager.close()
