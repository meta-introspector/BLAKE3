/-!
# Arm's XML, down to its ASL

Arm's A64 release gives each instruction's ASL, and the shared
pseudocode's, in `<pstext>` elements. Inside them Arm marks identifiers with
`<a ...>` and `<anchor ...>` tags and escapes three characters as `&amp;`,
`&lt;` and `&gt;`. `sections` returns every `<pstext>` element of a file as
its opening tag and its ASL text: tags dropped, entities decoded.

A file is a list of byte values (`Bytes`), as Bob makes it from his own
download with his own tool: Lean's kernel handles a string's characters
about a thousand times slower than numbers (README, "What Lean's kernel
computes"). The pass is one fold over the bytes, so the kernel reduces it
structurally.
-/

namespace Bob

/-- Text as byte values: what the kernel computes on quickly. -/
abbrev Bytes := List Nat

namespace Xml

/-- The pass's state, between two bytes. -/
structure State where
  /-- Inside a `<pstext>` element. -/
  inText : Bool := false
  /-- The bytes of a tag being read, reversed (after its `<`). -/
  tag : Option Bytes := none
  /-- The bytes of an entity being read, reversed (after its `&`). -/
  entity : Option Bytes := none
  /-- The current element's opening tag. -/
  opening : Bytes := []
  /-- The current element's text so far, reversed. -/
  text : Bytes := []
  /-- The finished elements, last first. -/
  done : List (Bytes × Bytes) := []

/-- `pre` is a prefix of `s`. -/
def startsWith : Bytes → Bytes → Bool
  | [], _ => true
  | p :: ps, c :: cs => p == c && startsWith ps cs
  | _ :: _, [] => false

/-- `pstext`, and `/pstext`. -/
def pstext : Bytes := [112, 115, 116, 101, 120, 116]
def endPstext : Bytes := 47 :: pstext

/-- An entity's byte (`amp`, `lt`, `gt`); any other is 0, which no ASL
text holds, so the parser rejects it. -/
def entityByte (name : Bytes) : Nat :=
  if name == [97, 109, 112] then 38
  else if name == [108, 116] then 60
  else if name == [103, 116] then 62
  else 0

/-- A tag ended (`tag` is its bytes, in order): it opens an element, closes
one, or (an `<a>` or `<anchor>` inside one) is dropped. -/
def endTag (st : State) (tag : Bytes) : State :=
  if st.inText then
    if tag == endPstext then
      { st with inText := false, tag := none, done := (st.opening, st.text.reverse) :: st.done }
    else { st with tag := none }
  else if startsWith pstext tag && (tag.drop 6 == [] || startsWith [32] (tag.drop 6)) then
    { st with inText := true, tag := none, opening := tag, text := [] }
  else { st with tag := none }

/-- One byte. -/
def step (st : State) (c : Nat) : State :=
  match st.tag, st.entity with
  | some t, _ => if c == 62 then endTag st t.reverse else { st with tag := some (c :: t) }
  | none, some e =>
    if c == 59 then { st with entity := none, text := entityByte e.reverse :: st.text }
    else { st with entity := some (c :: e) }
  | none, none =>
    if c == 60 then { st with tag := some [] }
    else if st.inText then
      if c == 38 then { st with entity := some [] } else { st with text := c :: st.text }
    else st

/-- Every `<pstext>` element of a file: its opening tag and its ASL text. -/
def sections (file : Bytes) : List (Bytes × Bytes) :=
  (file.foldl step {}).done.reverse

end Xml
end Bob
