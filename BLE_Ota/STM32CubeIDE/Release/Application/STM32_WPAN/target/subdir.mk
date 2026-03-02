################################################################################
# Automatically-generated file. Do not edit!
# Toolchain: GNU Tools for STM32 (13.3.rel1)
################################################################################

# Add inputs and outputs from these tool invocations to the build variables 
C_SRCS += \
N:/STM32/STM32Cube_FW_WB_V1.20.0/Projects/P-NUCLEO-WB55.Nucleo/Applications/BLE/BLE_Ota/STM32_WPAN/Target/hw_ipcc.c 

OBJS += \
./Application/STM32_WPAN/target/hw_ipcc.o 

C_DEPS += \
./Application/STM32_WPAN/target/hw_ipcc.d 


# Each subdirectory must supply rules for building sources it contributes
Application/STM32_WPAN/target/hw_ipcc.o: N:/STM32/STM32Cube_FW_WB_V1.20.0/Projects/P-NUCLEO-WB55.Nucleo/Applications/BLE/BLE_Ota/STM32_WPAN/Target/hw_ipcc.c Application/STM32_WPAN/target/subdir.mk
	arm-none-eabi-gcc "$<" -mcpu=cortex-m4 -std=gnu11 -DUSE_HAL_DRIVER -DUSE_STM32WBXX_NUCLEO -DCORE_CM4 -DSTM32WB55xx -c -I../../Core/Inc -I../../../../../../../Utilities/lpm/tiny_lpm -I../../../../../../../Middlewares/ST/STM32_WPAN/interface/patterns/ble_thread/shci -I../../../../../../../Utilities/sequencer -I../../../../../../../Drivers/CMSIS/Device/ST/STM32WBxx/Include -I../../../../../../../Middlewares/ST/STM32_WPAN/interface/patterns/ble_thread -I../../../../../../../Middlewares/ST/STM32_WPAN/interface/patterns/ble_thread/tl -I../../STM32_WPAN/App -I../../../../../../../Middlewares/ST/STM32_WPAN/ble/core -I../../../../../../../Middlewares/ST/STM32_WPAN -I../../../../../../../Middlewares/ST/STM32_WPAN/ble/core/template -I../../../../../../../Drivers/BSP/P-NUCLEO-WB55.Nucleo -I../../../../../../../Drivers/STM32WBxx_HAL_Driver/Inc -I../../../../../../../Middlewares/ST/STM32_WPAN/ble/core/auto -I../../../../../../../Middlewares/ST/STM32_WPAN/utilities -I../../../../../../../Middlewares/ST/STM32_WPAN/ble -I../../../../../../../Drivers/CMSIS/Include -Os -ffunction-sections -fdata-sections -Wall -fstack-usage -fcyclomatic-complexity -MMD -MP -MF"$(@:%.o=%.d)" -MT"$@" --specs=nano.specs -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -o "$@"

clean: clean-Application-2f-STM32_WPAN-2f-target

clean-Application-2f-STM32_WPAN-2f-target:
	-$(RM) ./Application/STM32_WPAN/target/hw_ipcc.cyclo ./Application/STM32_WPAN/target/hw_ipcc.d ./Application/STM32_WPAN/target/hw_ipcc.o ./Application/STM32_WPAN/target/hw_ipcc.su

.PHONY: clean-Application-2f-STM32_WPAN-2f-target

