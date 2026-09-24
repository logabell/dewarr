"""Address checks for outbound requests configured by untrusted users."""

import ipaddress


def public_address(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.sixtofour or address.teredo or address.is_site_local:
            return False
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return False
        if address.ipv4_mapped:
            return public_address(str(address.ipv4_mapped))
    return True
