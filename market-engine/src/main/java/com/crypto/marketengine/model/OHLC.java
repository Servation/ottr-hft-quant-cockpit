package com.crypto.marketengine.model;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@NoArgsConstructor
@AllArgsConstructor
public class OHLC {
    private double open;
    private double high;
    private double low;
    private double close;
    private double volume;
}
