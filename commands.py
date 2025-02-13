from enum import StrEnum


class Command(StrEnum):
    AUTH = "AUTH"
    ADD_VM = "ADD_VM"
    LIST_CONNECTED = "LIST_CONNECTED"
    LIST_AUTHORIZED = "LIST_AUTHORIZED"
    LIST_ALL = "LIST_ALL"
    LOGOUT = "LOGOUT"
    UPDATE_VM = "UPDATE_VM"
    LIST_DISKS = "LIST_DISKS"
    EXIT = "EXIT"