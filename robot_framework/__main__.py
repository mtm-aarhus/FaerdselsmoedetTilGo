"""The entry point of the process."""

# Denne robot behandler poster fra en OpenOrchestrator-kø (BBRSagsportalTransferToNova),
# ét køelement ad gangen - derfor queue_framework og ikke linear_framework
# (se README.md for forskellen).

from robot_framework import queue_framework
queue_framework.main()