# task_004_go_defer — leaked file handle in readLines

`readLines()` in `lines.go` opens a file but never closes it. Under the hood
the OS keeps each opened file alive until the program exits, so calling
`readLines()` in a loop leaks file descriptors. Add a `defer file.Close()`
right after the open call so the handle is released no matter how the
function returns.

## Expected behaviour

After calling `readLines()` a few hundred times, the process must not have
significantly more open file descriptors than before. The test asserts the
delta stays under a small threshold.

## How to verify

Run `bash test_command.sh` from this repo root. It must exit `0`.
