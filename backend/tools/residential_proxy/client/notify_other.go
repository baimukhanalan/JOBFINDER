//go:build !windows

package main

import (
	"fmt"
	"os/exec"
	"runtime"
)

func notify(title, msg string) {
	fmt.Println(title + ": " + msg)
	if runtime.GOOS == "darwin" {
		exec.Command("osascript", "-e",
			fmt.Sprintf("display notification %q with title %q", msg, title)).Run()
	}
}
