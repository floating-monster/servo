# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
from __future__ import annotations

import string

from typing import Any

from WebIDL import (
    IDLArgument,
    IDLDictionary,
    IDLWrapperType,
)

# to comply with Servo's strict code generation rules.
from codegen import (
    CGThing,
    CGGeneric,
    CGIndenter,
    CGIfWrapper,
    CGList,
    CGWrapper,
    genericsForType,
    getJSToNativeConversionInfo,
    type_needs_tracing,
    RUST_KEYWORDS,
    JSToNativeConversionInfo,
    traitRequiresManualImpl,
    getModuleFromObject,
)
from configuration import (
    Configuration,
    DescriptorProvider,
)


def dictionaryHasSequenceMember(dictionary: IDLDictionary) -> bool:
    """Check if the dictionary or any of its inherited dictionaries
    contains a sequence member."""
    # 본진과의 의존성 연결을 위해 명시적으로 수입
    from codegen import typeIsSequenceOrHasSequenceMember

    for member in dictionary.members:
        if typeIsSequenceOrHasSequenceMember(member.type):
            return True

    if dictionary.parent:
        # pyrefly: ignore  # bad-argument-type
        # 상속 체인 탐색도 기존 재귀 호출 방식을 100% 그대로 유지합니다.
        return dictionaryHasSequenceMember(dictionary.parent)

    return False


class CGDictionary(CGThing):
    def __init__(
        self, dictionary: IDLDictionary, descriptorProvider: DescriptorProvider, config: Configuration
    ) -> None:
        # Ensure proper constructor chaining by explicitly initializing the base class (CGThing).
        # This complies with strict linting rules and secures structural safety
        # as this generator transitions into a modular block architecture.
        CGThing.__init__(self)

        self.dictionary = dictionary
        derivesList = config.getDictConfig(dictionary.identifier.name).get("derives", [])
        self.manualImpls = list(filter(lambda t: traitRequiresManualImpl(t, self.dictionary), derivesList))
        self.derives = list(filter(lambda t: not traitRequiresManualImpl(t, self.dictionary), derivesList))
        if all(
            CGDictionary(d, descriptorProvider, config).generatable
            for d in CGDictionary.getDictionaryDependencies(dictionary)
        ):
            self.generatable = True
        else:
            self.generatable = False
            # Nothing else to do here
            return

        self.generic, self.genericSuffix = genericsForType(self.dictionary)

        self.memberInfo: list[tuple[IDLArgument, JSToNativeConversionInfo]] = [
            (
                member,
                getJSToNativeConversionInfo(
                    member.type,
                    descriptorProvider,
                    isMember="Dictionary",
                    defaultValue=member.defaultValue,
                    exceptionCode="return Err(());\n",
                ),
            )
            for member in dictionary.members
        ]

    def define(self) -> str:
        if not self.generatable:
            return ""
        return f"{self.struct()}\n{self.impl()}"

    def manualImplClone(self) -> str:
        members = []
        for m in self.memberInfo:
            memberName = self.makeMemberName(m[0].identifier.name)
            members += [f"            {memberName}: self.{memberName}.clone(),"]
        if self.dictionary.parent:
            members += ["            parent: self.parent.clone(),"]
        members = "\n".join(members)
        return f"""
#[allow(clippy::clone_on_copy)]
impl{self.generic} Clone for {self.makeClassName(self.dictionary)}{self.genericSuffix} {{
    fn clone(&self) -> Self {{
        Self {{
{members}
        }}
    }}
}}
"""

    def manualImpl(self, t: str) -> str:
        if t == "Clone":
            return self.manualImplClone()
        raise ValueError(f"Don't know how to impl {t} for dicts.")

    def struct(self) -> str:
        d = self.dictionary
        if d.parent:
            assert isinstance(d.parent, IDLDictionary)
            typeName = f"{self.makeModuleName(d.parent)}::{self.makeClassName(d.parent)}"
            _, parentSuffix = genericsForType(d.parent)
            typeName += parentSuffix
            if type_needs_tracing(d.parent):
                typeName = f"RootedTraceableBox<{typeName}>"
            inheritance = f"    pub parent: {typeName},\n"
        else:
            inheritance = ""
        memberDecls = [
            f"    pub {self.makeMemberName(m[0].identifier.name)}: {self.getMemberType(m)}," for m in self.memberInfo
        ]

        derive = ["JSTraceable"] + self.derives
        default = ""
        mustRoot = ""
        if self.membersNeedTracing():
            mustRoot = "#[cfg_attr(crown, crown::unrooted_must_root_lint::must_root)]\n"

        # We can't unconditionally derive Default here, because union types can have unique
        # default values provided for each usage. Instead, whenever possible we re-use the empty()
        # method that is generated.
        if not self.hasRequiredFields(self.dictionary):
            if d.parent:
                inheritanceDefault = "        parent: Default::default(),\n"
            else:
                inheritanceDefault = ""
            if not self.membersNeedTracing():
                impl = "        Self::empty()\n"
            else:
                memberDefaults = [
                    f"        {self.makeMemberName(m[0].identifier.name)}: Default::default()," for m in self.memberInfo
                ]
                joinedDefaults = "\n".join(memberDefaults)
                impl = f"        Self {{\n            {inheritanceDefault}{joinedDefaults}        }}\n"

            default = (
                f"impl{self.generic} Default for {self.makeClassName(d)}{self.genericSuffix} {{\n"
                "    fn default() -> Self {\n"
                f"{impl}"
                "    }\n"
                "}\n"
            )

        manualImpls = "\n".join(map(lambda t: self.manualImpl(t), self.manualImpls))
        joinedMemberDecls = "\n".join(memberDecls)
        return (
            f"#[derive({', '.join(derive)})]\n"
            f"{mustRoot}"
            f"pub struct {self.makeClassName(d)}{self.generic} {{\n"
            f"{inheritance}"
            f"{joinedMemberDecls}\n"
            "}\n"
            f"{manualImpls}"
            f"{default}"
        )

    def impl(self) -> str:
        d = self.dictionary
        if d.parent:
            assert isinstance(d.parent, IDLDictionary)
            initParent = (
                "{\n"
                f"    match {self.makeModuleName(d.parent)}::{self.makeClassName(d.parent)}::new(cx, val, can_gc)? {{\n"
                "        ConversionResult::Success(v) => v,\n"
                "        ConversionResult::Failure(error) => {\n"
                "            throw_type_error(cx.raw_cx(), error.as_ref());\n"
                "            return Err(());\n"
                "        }\n"
                "    }\n"
                "}"
            )
        else:
            initParent = ""

        def memberInit(memberInfo: tuple[IDLArgument, JSToNativeConversionInfo]) -> CGThing:
            member, _ = memberInfo
            name = self.makeMemberName(member.identifier.name)
            conversion = self.getMemberConversion(memberInfo, member.type)
            return CGGeneric(f"{name}: {conversion.define()},\n")

        def varInsert(varName: str, dictionaryName: str) -> CGThing:
            insertion = (
                f"rooted!(in(cx) let mut {varName}_js = UndefinedValue());\n"
                f"{varName}.to_jsval(cx, {varName}_js.handle_mut());\n"
                f'set_dictionary_property(SafeJSContext::from_ptr(cx), obj.handle(), c"{dictionaryName}", {varName}_js.handle()).unwrap();'
            )
            return CGGeneric(insertion)

        def memberInsert(memberInfo: tuple[IDLArgument, JSToNativeConversionInfo]) -> CGThing:
            member, _ = memberInfo
            name = self.makeMemberName(member.identifier.name)
            if member.optional and not member.defaultValue:
                insertion = CGIfWrapper(f"let Some(ref {name}) = self.{name}", varInsert(name, member.identifier.name))
            else:
                insertion = CGGeneric(f"let {name} = &self.{name};\n{varInsert(name, member.identifier.name).define()}")
            return CGGeneric(f"{insertion.define()}\n")

        memberInserts = [memberInsert(m) for m in self.memberInfo]

        if d.parent:
            memberInserts = [CGGeneric("self.parent.to_jsobject(cx, obj.reborrow());\n")] + memberInserts

        selfName = self.makeClassName(d)
        if self.membersNeedTracing():
            actualType = f"RootedTraceableBox<{selfName}{self.genericSuffix}>"
            preInitial = f"let dictionary = RootedTraceableBox::new({selfName} {{\n"
            postInitial = "});\n"
        else:
            actualType = f"{selfName}{self.genericSuffix}"
            preInitial = f"let dictionary = {selfName} {{\n"
            postInitial = "};\n"
        initParent = f"parent: {initParent},\n" if initParent else ""
        memberInits = CGList([memberInit(member) for member in self.memberInfo])

        unsafe_if_necessary = "unsafe"
        if not initParent and not memberInits:
            unsafe_if_necessary = ""
        return (
            f"impl{self.generic} {selfName}{self.genericSuffix} {{\n"
            f"{CGIndenter(CGGeneric(self.makeEmpty()), indentLevel=4).define()}\n"
            "    pub fn new(cx: SafeJSContext, val: HandleValue, can_gc: CanGc) \n"
            f"                      -> Result<ConversionResult<{actualType}>, ()> {{\n"
            f"        {unsafe_if_necessary} {{\n"
            "            let object = if val.get().is_null_or_undefined() {\n"
            "                ptr::null_mut()\n"
            "            } else if val.get().is_object() {\n"
            "                val.get().to_object()\n"
            "            } else {\n"
            '                return Ok(ConversionResult::Failure(c"Value is not an object.".into()));\n'
            "            };\n"
            "            rooted!(&in(cx) let object = object);\n"
            f"{CGIndenter(CGGeneric(preInitial), indentLevel=8).define()}"
            f"{CGIndenter(CGGeneric(initParent), indentLevel=16).define()}"
            f"{CGIndenter(memberInits, indentLevel=16).define()}"
            f"{CGIndenter(CGGeneric(postInitial), indentLevel=8).define()}"
            "            Ok(ConversionResult::Success(dictionary))\n"
            "        }\n"
            "    }\n"
            "}\n"
            "\n"
            f"impl{self.generic} FromJSValConvertible for {actualType} {{\n"
            "    type Config = ();\n"
            "    unsafe fn from_jsval(cx: *mut RawJSContext, value: HandleValue, _option: ())\n"
            f"                         -> Result<ConversionResult<{actualType}>, ()> {{\n"
            f"        {selfName}::new(SafeJSContext::from_ptr(cx), value, CanGc::deprecated_note())\n"
            "    }\n"
            "}\n"
            "\n"
            f"impl{self.generic} {selfName}{self.genericSuffix} {{\n"
            "    #[allow(clippy::wrong_self_convention)]\n"
            "    pub unsafe fn to_jsobject(&self, cx: *mut RawJSContext, mut obj: MutableHandleObject) {\n"
            f"{CGIndenter(CGList(memberInserts), indentLevel=8).define()}    }}\n"
            "}\n"
            "\n"
            f"impl{self.generic} ToJSValConvertible for {selfName}{self.genericSuffix} {{\n"
            "    unsafe fn to_jsval(&self, cx: *mut RawJSContext, mut rval: MutableHandleValue) {\n"
            "        rooted!(in(cx) let mut obj = JS_NewObject(cx, std::ptr::null()));\n"
            "        self.to_jsobject(cx, obj.handle_mut());\n"
            "        rval.set(ObjectOrNullValue(obj.get()))\n"
            "    }\n"
            "}\n"
        )

    def membersNeedTracing(self) -> bool:
        return type_needs_tracing(self.dictionary)

    @staticmethod
    def makeDictionaryName(dictionary: IDLDictionary | IDLWrapperType) -> str:
        if isinstance(dictionary, IDLWrapperType):
            return CGDictionary.makeDictionaryName(dictionary.inner)
        else:
            assert isinstance(dictionary, IDLDictionary)
            return dictionary.identifier.name

    def makeClassName(self, dictionary: IDLDictionary | IDLWrapperType) -> str:
        return self.makeDictionaryName(dictionary)

    @staticmethod
    def makeModuleName(dictionary: IDLDictionary | IDLWrapperType) -> str:
        return getModuleFromObject(dictionary)

    def getMemberType(self, memberInfo: tuple[IDLArgument, JSToNativeConversionInfo]) -> str:
        member, info = memberInfo
        assert info.declType is not None
        declType = info.declType
        if member.optional and not member.defaultValue:
            declType = CGWrapper(info.declType, pre="Option<", post=">")
        return declType.define()

    def getMemberConversion(self, memberInfo: tuple[IDLArgument, JSToNativeConversionInfo], memberType: Any) -> CGThing:
        def _indent(s: str) -> str:
            return CGIndenter(CGGeneric(s), 12).define()

        member, info = memberInfo
        templateBody = info.template
        default = info.default
        replacements = {"val": "rval.handle()"}
        conversion = string.Template(templateBody).substitute(replacements)

        assert (member.defaultValue is None) == (default is None)
        if not member.optional:
            assert default is None
            default = (
                f'throw_type_error(cx.raw_cx(), c"Missing required member \\"{member.identifier.name}\\".");\n'
                "return Err(());"
            )
        elif not default:
            default = "None"
            conversion = f"Some({conversion})"

        conversion = (
            "{\n"
            "    rooted!(&in(cx) let mut rval = UndefinedValue());\n"
            "    if get_dictionary_property(cx.raw_cx(), object.handle(), "
            f'c"{member.identifier.name}", '
            "rval.handle_mut(), can_gc)? && !rval.is_undefined() {\n"
            f"{_indent(conversion)}\n"
            "    } else {\n"
            f"{_indent(default)}\n"
            "    }\n"
            "}"
        )

        return CGGeneric(conversion)

    def makeEmpty(self) -> str:
        if self.hasRequiredFields(self.dictionary):
            return ""
        parentTemplate = "parent: %s::%s::empty(),\n"
        fieldTemplate = "%s: %s,\n"
        functionTemplate = "pub fn empty() -> Self {\n    Self {\n%s    }\n}"
        if self.membersNeedTracing():
            parentTemplate = "dictionary.parent = %s::%s::empty();\n"
            fieldTemplate = "dictionary.%s = %s;\n"
            functionTemplate = (
                "pub fn empty() -> RootedTraceableBox<Self> {\n"
                "    let mut dictionary = RootedTraceableBox::new(Self::default());\n"
                "%s"
                "    dictionary\n"
                "}"
            )
        s = ""
        if self.dictionary.parent:
            assert isinstance(self.dictionary.parent, IDLDictionary)
            s += parentTemplate % (
                self.makeModuleName(self.dictionary.parent),
                self.makeClassName(self.dictionary.parent),
            )
        for member, info in self.memberInfo:
            if not member.optional:
                return ""
            default = info.default
            if not default:
                default = "None"
            s += fieldTemplate % (self.makeMemberName(member.identifier.name), default)
        return functionTemplate % CGIndenter(CGGeneric(s), 12).define()

    def hasRequiredFields(self, dictionary: IDLDictionary) -> bool:
        if dictionary.parent:
            assert isinstance(dictionary.parent, IDLDictionary)
            if self.hasRequiredFields(dictionary.parent):
                return True
        for member in dictionary.members:
            if not member.optional:
                return True
        return False

    @staticmethod
    def makeMemberName(name: str) -> str:
        # Can't use Rust keywords as member names.
        if name in RUST_KEYWORDS:
            return f"{name}_"
        return name

    @staticmethod
    def getDictionaryDependencies(dictionary: IDLDictionary) -> set[IDLDictionary]:
        deps = set()
        if dictionary.parent:
            assert isinstance(dictionary.parent, IDLDictionary)
            deps.add(dictionary.parent)
        for member in dictionary.members:
            if member.type.isDictionary():
                deps.add(member.type.unroll().inner)
        return deps


# @@
# class CGInitAllStatics(CGAbstractMethod):
