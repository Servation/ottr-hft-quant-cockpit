package com.crypto.marketengine.model;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@NoArgsConstructor
@AllArgsConstructor
@Builder
public class ExecutionResult {
    private String orderId;
    private String symbol;
    private String side;
    private double requestedQuantity;
    private double filledQuantity;
    private double vwapPrice;
    private double slippage;
    private double feeDeducted;
    private String status; // "FILLED", "PARTIALLY_FILLED", "REJECTED", "EXPIRED"
    private long executionTimeMs;
    private long targetExecutionTimeMs;
    private long actualLatencyMs;
}
