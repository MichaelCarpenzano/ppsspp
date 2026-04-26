// Copyright (c) 2024- PPSSPP Project.

// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, version 2.0 or later versions.

// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License 2.0 for more details.

// A copy of the GPL 2.0 should have been included with the program.
// If not, see http://www.gnu.org/licenses/

// Official git repository and contact information can be found at
// https://github.com/hrydgard/ppsspp and http://www.ppsspp.org/.

// SaveState WebSocket subscriber
//
// Exposes in-memory savestate slots (0-4) via the WebSocket debugger, enabling
// external tools and AI agents to checkpoint and restore emulator state.
//
// Commands:
//   savestate.save  — serialize current PSP state into an in-memory slot
//   savestate.load  — restore state from an in-memory slot
//   savestate.list  — enumerate which slots are populated
//
// All commands require the CPU to be in stepping mode.

#include <array>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <vector>

#include "Core/Core.h"
#include "Core/ELF/ParamSFO.h"
#include "Core/System.h"
#include "Core/MIPS/MIPSDebugInterface.h"
#include "Core/Debugger/WebSocket/SaveStateSubscriber.h"
#include "Core/Debugger/WebSocket/WebSocketUtils.h"
#include "Core/SaveState.h"

static const int SAVESTATE_SLOT_COUNT = 5;

using SaveStateSlots = std::array<std::vector<uint8_t>, SAVESTATE_SLOT_COUNT>;

static std::mutex g_inMemorySlotsLock;
// In-memory save slots, namespaced by current game/disc ID.
static std::map<std::string, SaveStateSlots> g_inMemorySlotsByGame;

static std::string CurrentGameKey() {
	const std::string discID = g_paramSFO.GetDiscID();
	if (!discID.empty())
		return discID;
	return "__unknown_game__";
}

DebuggerSubscriber *WebSocketSaveStateInit(DebuggerEventHandlerMap &map) {
	map["savestate.save"] = &WebSocketSaveStateSave;
	map["savestate.load"] = &WebSocketSaveStateLoad;
	map["savestate.list"] = &WebSocketSaveStateList;
	return nullptr;
}

// Save current emulator state to an in-memory slot (savestate.save)
//
// Parameters:
//  - slot: integer 0-4, the slot to save into.
//
// Response (same event name):
//  - slot: integer, the slot that was saved.
//  - size: integer, bytes used by the serialized state.
void WebSocketSaveStateSave(DebuggerRequest &req) {
	if (!currentDebugMIPS->isAlive())
		return req.Fail("CPU not started");
	if (!Core_IsStepping())
		return req.Fail("CPU must be in stepping mode to save state (call cpu.stepping first)");

	uint32_t slot = 0;
	if (!req.ParamU32("slot", &slot))
		return;
	if (slot >= SAVESTATE_SLOT_COUNT)
		return req.Fail("Slot must be 0-4");

	std::vector<uint8_t> buf;
	CChunkFileReader::Error err = SaveState::SaveToRam(buf);
	if (err != CChunkFileReader::ERROR_NONE)
		return req.Fail("SaveToRam failed");

	const std::string gameKey = CurrentGameKey();
	const int size = static_cast<int>(buf.size());
	{
		std::lock_guard<std::mutex> guard(g_inMemorySlotsLock);
		g_inMemorySlotsByGame[gameKey][slot] = std::move(buf);
	}

	JsonWriter &json = req.Respond();
	json.writeInt("slot", static_cast<int>(slot));
	json.writeInt("size", size);
}

// Load emulator state from an in-memory slot (savestate.load)
//
// Parameters:
//  - slot: integer 0-4, the slot to load from.
//
// Response (same event name):
//  - slot: integer, the slot that was loaded.
//  - size: integer, bytes restored.
void WebSocketSaveStateLoad(DebuggerRequest &req) {
	if (!currentDebugMIPS->isAlive())
		return req.Fail("CPU not started");
	if (!Core_IsStepping())
		return req.Fail("CPU must be in stepping mode to load state (call cpu.stepping first)");

	uint32_t slot = 0;
	if (!req.ParamU32("slot", &slot))
		return;
	if (slot >= SAVESTATE_SLOT_COUNT)
		return req.Fail("Slot must be 0-4");

	const std::string gameKey = CurrentGameKey();
	std::vector<uint8_t> buf;
	{
		std::lock_guard<std::mutex> guard(g_inMemorySlotsLock);
		auto gameIt = g_inMemorySlotsByGame.find(gameKey);
		if (gameIt == g_inMemorySlotsByGame.end() || gameIt->second[slot].empty())
			return req.Fail("Slot is empty");
		buf = gameIt->second[slot];
	}

	std::string errorString;
	CChunkFileReader::Error err = SaveState::LoadFromRam(buf, &errorString);
	if (err != CChunkFileReader::ERROR_NONE)
		return req.Fail("LoadFromRam failed: " + errorString);

	JsonWriter &json = req.Respond();
	json.writeInt("slot", static_cast<int>(slot));
	json.writeInt("size", static_cast<int>(buf.size()));
}

// List populated in-memory savestate slots (savestate.list)
//
// No parameters.
//
// Response (same event name):
//  - slots: array of objects, one per slot 0-4, each with:
//     - slot: integer
//     - occupied: boolean
//     - size: integer (0 if empty)
void WebSocketSaveStateList(DebuggerRequest &req) {
	std::array<int, SAVESTATE_SLOT_COUNT> sizes{};
	{
		std::lock_guard<std::mutex> guard(g_inMemorySlotsLock);
		auto gameIt = g_inMemorySlotsByGame.find(CurrentGameKey());
		if (gameIt != g_inMemorySlotsByGame.end()) {
			for (int i = 0; i < SAVESTATE_SLOT_COUNT; ++i) {
				sizes[i] = static_cast<int>(gameIt->second[i].size());
			}
		}
	}

	JsonWriter &json = req.Respond();
	json.pushArray("slots");
	for (int i = 0; i < SAVESTATE_SLOT_COUNT; ++i) {
		json.pushDict();
		json.writeInt("slot", i);
		json.writeBool("occupied", sizes[i] != 0);
		json.writeInt("size", sizes[i]);
		json.pop();
	}
	json.pop();
}
