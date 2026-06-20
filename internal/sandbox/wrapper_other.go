//go:build !android

package sandbox

// wrapArgv is a no-op on non-Android platforms.
func wrapArgv(argv []string) []string { return argv }
