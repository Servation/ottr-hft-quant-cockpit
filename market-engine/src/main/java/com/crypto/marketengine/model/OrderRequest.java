package com.crypto.marketengine.model;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@NoArgsConstructor
@AllArgsConstructor
public class OrderRequest {
    private String symbol;
    private String side; // "BUY" or "SELL"
    private String type; // "LIMIT" or "MARKET"
    private double quantity;
    private double price; // for LIMIT orders
}
