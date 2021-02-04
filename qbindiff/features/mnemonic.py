from qbindiff.features.visitor import InstructionFeature, Environment
from qbindiff.loader.instruction import Instruction


class MnemonicSimple(InstructionFeature):
    """Mnemonic of instructions feature"""
    name = "mnemonic"
    key = "M"

    def visit_instruction(self, instruction: Instruction, env: Environment):
        env.inc_feature(instruction.mnemonic)


class MnemonicTyped(InstructionFeature):
    """Mnemonic and type of operand feature"""
    name = "mnemonic_typed"
    key = "Mt"

    def visit_instruction(self, instruction: Instruction, env: Environment):
        keycode = ''.join(str(x.type.value) for x in instruction.operands)
        env.inc_feature(instruction.mnemonic+keycode)


class GroupsCategory(InstructionFeature):
    """Group of the instruction (FPU, SSE, stack..)"""
    name = "groups_category"
    key = "Gp"

    def visit_instruction(self, instruction: Instruction, env: Environment):
        for g in instruction.groups:
            if g not in ['UNDEFINED', 'NOTINCS', 'NOTINIDA', 'DEPRECATED']:
                env.inc_feature(g)
