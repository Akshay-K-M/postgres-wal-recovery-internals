# Demo breakpoints for the runtime abort path.
# Load with:  gdb -p <backend pid> -x /workspaces/postgresql/recovery_app/initial_experiments/demo.gdb
# At every stop the key values print automatically; type  c  to continue.
set pagination off
set confirm off
set print pretty off

break int4div
commands
  bt 6
end

break AbortCurrentTransaction
commands
  p CurrentTransactionState->blockState
  bt 2
end

break UserAbortTransactionBlock
commands
  p CurrentTransactionState->blockState
end

break AbortTransaction
commands
  p CurrentTransactionState->blockState
  bt 3
end

break RecordTransactionAbort
commands
  p CurrentTransactionState->fullTransactionId.value
end

break XactLogAbortRecord

break TransactionIdAbortTree
commands
  p xid
  printf "abort WAL record written: start %X/%08X  end %X/%08X\n", (unsigned int)(ProcLastRecPtr >> 32), (unsigned int)ProcLastRecPtr, (unsigned int)(XactLastRecEnd >> 32), (unsigned int)XactLastRecEnd
end

break TransactionIdSetStatusBit
commands
  p xid
  p status
  bt 8
end

break CleanupTransaction
commands
  p CurrentTransactionState->state
  p CurrentTransactionState->blockState
end

echo \n*** demo.gdb loaded: 9 breakpoints. Type  c  to let the backend run. ***\n
