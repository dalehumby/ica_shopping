DOMAIN = "ica_shopping"

API_USER_INFO = "https://www.ica.se/api/user/information"
API_LIST_ALL = "https://apimgw-pub.ica.se/sverige/digx/shopping-list/v1/api/list/all"
API_ADD_ROW = "https://apimgw-pub.ica.se/sverige/digx/shopping-list/v1/api/list/{list_id}/row"
API_ROW = "https://apimgw-pub.ica.se/sverige/digx/shopping-list/v1/api/row/{row_id}"


def signal_list_updated(entry_id: str) -> str:
    """Dispatcher signal carrying the latest ICA list after a sync."""
    return f"{DOMAIN}_{entry_id}_list_updated"


def signal_session_updated(entry_id: str) -> str:
    """Dispatcher signal sent when the ICA session becomes valid/invalid."""
    return f"{DOMAIN}_{entry_id}_session_updated"
