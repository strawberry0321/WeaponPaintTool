// Read the declared resources from a local Helldivers 2 installation.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"github.com/xypwn/filediver/stingray"
)

type resource struct {
	ID       string            `json:"id"`
	Kind     string            `json:"kind"`
	Sections map[string]string `json:"sections"`
}

var sections = map[string]stingray.DataType{
	"main": stingray.DataMain, "gpu": stingray.DataGPU, "stream": stingray.DataStream,
}

func extract(directory *stingray.DataDir, entry resource, output string) error {
	value, err := strconv.ParseUint(entry.ID, 16, 64)
	if err != nil || len(entry.ID) != 16 {
		return errors.New("invalid resource ID")
	}
	switch entry.Kind {
	case "unit", "material", "texture":
	default:
		return errors.New("invalid resource kind")
	}
	id := stingray.NewFileID(stingray.Hash{Value: value}, stingray.Sum(entry.Kind))
	original := directory.Files[id]
	defer func() { directory.Files[id] = original }()
	for _, candidate := range original {
		directory.Files[id] = []stingray.FileInfo{candidate}
		matched := make(map[string][]byte)
		for suffix, want := range entry.Sections {
			kind, ok := sections[suffix]
			if !ok {
				return errors.New("invalid resource section")
			}
			data, err := directory.Read(id, kind)
			sum := sha256.Sum256(data)
			if err != nil || hex.EncodeToString(sum[:]) != want {
				break
			}
			matched[suffix] = data
		}
		if len(matched) != len(entry.Sections) {
			continue
		}
		folder := filepath.Join(output, entry.Kind)
		if err := os.MkdirAll(folder, 0755); err != nil {
			return err
		}
		for suffix, data := range matched {
			name := fmt.Sprintf("%016x.%s.%s", value, entry.Kind, suffix)
			if err := os.WriteFile(filepath.Join(folder, name), data, 0644); err != nil {
				return err
			}
		}
		return nil
	}
	return fmt.Errorf("missing compatible %s %s", entry.Kind, entry.ID)
}

func run() error {
	if len(os.Args) != 4 {
		return errors.New("usage: stock_extract GAME_DIRECTORY MANIFEST OUTPUT")
	}
	var required []resource
	body, err := os.ReadFile(os.Args[2])
	if err != nil {
		return err
	}
	if err := json.Unmarshal(body, &required); err != nil {
		return err
	}
	directory, err := stingray.OpenDataDir(context.Background(), filepath.Join(os.Args[1], "data"), nil)
	if err != nil {
		return errors.New("could not read the game data index")
	}
	var failures []error
	for _, entry := range required {
		if err := extract(directory, entry, os.Args[3]); err != nil {
			failures = append(failures, err)
		}
	}
	return errors.Join(failures...)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
