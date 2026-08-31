//go:build windows

package main

import (
	"os/exec"
	"syscall"
)

// Windows creation flags (not exported by syscall on all versions).
const (
	_DETACHED_PROCESS        = 0x00000008
	_CREATE_NEW_PROCESS_GROUP = 0x00000200
)

// startDetached launches "<ap> --run" as a detached, window-less background process that outlives this
// installer process. DETACHED_PROCESS gives it no console; CREATE_NEW_PROCESS_GROUP unhooks it from the
// parent's Ctrl-C group so closing the installer never kills the tunnel.
func startDetached(ap string) error {
	cmd := exec.Command(ap, "--run")
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
	}
	return cmd.Start()
}
