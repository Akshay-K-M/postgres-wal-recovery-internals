# Runtime abort path breakpoints (Week 1, part A).
# Usage: gdb -p <backend pid>, then type:  source recovery_app/initial_experiments/abort.gdb
# (type it by hand -- pasting into gdb in the VS Code terminal adds \r)
set pagination off
set confirm off
delete
break int4div
break AbortCurrentTransaction
break UserAbortTransactionBlock
break AbortTransaction
break RecordTransactionAbort
break XactLogAbortRecord
break TransactionIdAbortTree
break TransactionIdSetStatusBit
break CleanupTransaction
info breakpoints
