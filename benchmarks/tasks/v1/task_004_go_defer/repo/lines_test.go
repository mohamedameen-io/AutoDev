package main

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

// TestReadLinesDoesNotLeakFDs lowers the per-process open-file limit and
// then calls readLines() many times. With the leak, we run out of fds and
// either Open or scanner returns an error. With defer file.Close(), it
// passes cleanly.
func TestReadLinesDoesNotLeakFDs(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "input.txt")
	if err := os.WriteFile(path, []byte("a\nb\nc\n"), 0o644); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}

	// Lower RLIMIT_NOFILE so the leak shows up well within the loop.
	var orig syscall.Rlimit
	if err := syscall.Getrlimit(syscall.RLIMIT_NOFILE, &orig); err != nil {
		t.Fatalf("Getrlimit: %v", err)
	}
	low := syscall.Rlimit{Cur: 64, Max: orig.Max}
	if err := syscall.Setrlimit(syscall.RLIMIT_NOFILE, &low); err != nil {
		t.Skipf("cannot lower RLIMIT_NOFILE on this platform: %v", err)
	}
	t.Cleanup(func() {
		_ = syscall.Setrlimit(syscall.RLIMIT_NOFILE, &orig)
	})

	const iterations = 500
	for i := 0; i < iterations; i++ {
		if _, err := readLines(path); err != nil {
			t.Fatalf("readLines failed at iteration %d (likely fd leak): %v", i, err)
		}
	}
	fmt.Printf("readLines completed %d iterations under RLIMIT_NOFILE=%d\n",
		iterations, low.Cur)
}
