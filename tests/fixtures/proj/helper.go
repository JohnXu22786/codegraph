package helper

import "strings"

func Greet(name string) string {
	return "hello " + strings.ToUpper(name)
}
