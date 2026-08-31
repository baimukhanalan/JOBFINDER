//go:build !windows

package main

import "os/exec"

// startDetached launches "<ap> --run" in the background. On mac/linux the child already survives the
// parent exiting; autostart (launchd/none) handles reboots.
func startDetached(ap string) error {
	return exec.Command(ap, "--run").Start()
}
