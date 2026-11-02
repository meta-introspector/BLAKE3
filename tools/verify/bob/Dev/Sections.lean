import Bob.Xml
/-! Development only (none of Bob's check): print a file's sections, for
comparison with an independent extraction. -/
def main (args : List String) : IO Unit := do
  let bytes ← IO.FS.readBinFile args[0]!
  for (opening, text) in Bob.Xml.sections (bytes.toList.map (·.toNat)) do
    IO.println s!"== {String.fromUTF8! (ByteArray.mk (opening.map (·.toUInt8)).toArray)}"
    IO.println (String.fromUTF8! (ByteArray.mk (text.map (·.toUInt8)).toArray))
